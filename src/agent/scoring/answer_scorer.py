"""回答质量打分"""

from __future__ import annotations

import logging
import math
import re

from config.settings import settings
from src.agent.models import AnswerEval
from src.llm.openai_client import LLMClient
from src.rag.models import RetrievedChunk

logger = logging.getLogger(__name__)

CAUTION_QUERY_RE = re.compile(r"注意|要注意|注意事项")
NUMBERED_RE = re.compile(r"[1-9][、.)．]")


def _extract_terms(text: str, min_len: int = 2) -> set[str]:
    terms: set[str] = set()
    for bg in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        if len(bg) >= min_len:
            terms.add(bg)
    return terms


def _faithfulness_heuristic(answer: str, hits: list[RetrievedChunk]) -> float:
    if not hits:
        return 0.0 if answer.strip() else 1.0
    context = "\n".join(h.text for h in hits)
    answer_terms = _extract_terms(answer)
    if not answer_terms:
        return 0.5
    context_terms = _extract_terms(context)
    overlap = sum(1 for t in answer_terms if t in context or t in context.replace(" ", ""))
    return overlap / len(answer_terms)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _completeness_heuristic(question: str, answer: str) -> float:
    if not answer.strip():
        return 0.0
    if CAUTION_QUERY_RE.search(question):
        numbered = len(NUMBERED_RE.findall(answer))
        if numbered >= 3:
            return 1.0
        if numbered >= 1 or "注意" in answer:
            return 0.65
        return 0.35
    if len(answer) >= 80:
        return 0.85
    if len(answer) >= 30:
        return 0.7
    return 0.5


class AnswerScorer:
    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm
        # Embedder 懒加载（优化 3）
        self._embedder = None
        self._embedder_unavailable = False

    def _get_embedder(self):
        """懒加载 Embedder；不可用时返回 None。"""
        if self._embedder_unavailable:
            return None
        if self._embedder is None:
            try:
                from src.rag.embedder import Embedder

                self._embedder = Embedder()
            except Exception as e:
                logger.debug("Embedder 不可用，忠实度回退纯 bigram: %s", e)
                self._embedder_unavailable = True
                return None
        return self._embedder

    def _embedding_faithfulness(
        self, answer: str, hits: list[RetrievedChunk]
    ) -> float:
        """回答与检索上下文的 embedding 余弦相似度（优化 3）。

        不可用或异常返回 -1.0，调用方回退到纯 bigram。
        """
        embedder = self._get_embedder()
        if embedder is None:
            return -1.0
        context = "\n".join(h.text for h in hits)[:8000]
        try:
            vecs = embedder.embed_texts([answer, context])
            if len(vecs) < 2:
                return -1.0
            return _cosine_similarity(vecs[0], vecs[1])
        except Exception as e:
            logger.debug("embedding faithfulness 计算失败: %s", e)
            return -1.0

    def score(
        self,
        question: str,
        hits: list[RetrievedChunk],
        answer: str,
        *,
        skip_llm_judge: bool = False,
    ) -> AnswerEval:
        faith_h = _faithfulness_heuristic(answer, hits)
        # Embedding 忠实度融合（优化 3）
        if getattr(settings, "agent_embedding_faithfulness_enabled", True) and hits:
            emb_faith = self._embedding_faithfulness(answer, hits)
            if emb_faith >= 0.0:
                weight = float(
                    getattr(settings, "agent_embedding_faithfulness_weight", 0.4) or 0.4
                )
                faith_h = (1.0 - weight) * faith_h + weight * emb_faith
        comp_h = _completeness_heuristic(question, answer)
        grounded = faith_h >= 0.35 or not hits

        faithfulness = faith_h
        completeness = comp_h
        missing: list[str] = []
        feedback = ""

        heuristic_pass = (
            faithfulness >= settings.agent_answer_faithfulness_threshold
            and completeness >= settings.agent_answer_completeness_threshold
            and grounded
        )

        use_llm = settings.agent_enable_llm_judge and (
            settings.agent_llm_judge_always
            or (
                not skip_llm_judge
                and (
                    faith_h < settings.agent_answer_llm_threshold
                    or CAUTION_QUERY_RE.search(question)
                    or not heuristic_pass
                )
            )
        )
        # 高置信快路径：启发式已过线则跳过 LLM judge
        if skip_llm_judge and heuristic_pass and not settings.agent_llm_judge_always:
            use_llm = False

        if use_llm:
            llm_eval = self._llm_judge(question, hits, answer)
            if llm_eval:
                faithfulness = llm_eval.get("faithfulness", faith_h)
                completeness = llm_eval.get("completeness", comp_h)
                grounded = llm_eval.get("grounded", grounded)
                missing = llm_eval.get("missing_points", [])
                feedback = llm_eval.get("feedback", "")

        score = 0.6 * faithfulness + 0.4 * completeness
        if not grounded:
            score *= 0.5

        passed = (
            faithfulness >= settings.agent_answer_faithfulness_threshold
            and completeness >= settings.agent_answer_completeness_threshold
            and grounded
        )

        if not passed and not feedback:
            parts: list[str] = []
            if faithfulness < settings.agent_answer_faithfulness_threshold:
                parts.append("回答与检索片段贴合度不足")
            if completeness < settings.agent_answer_completeness_threshold:
                parts.append("回答不够完整")
            if not grounded:
                parts.append("回答可能包含检索片段未提及的内容")
            feedback = "；".join(parts) + "，请依据检索片段重写。"

        return AnswerEval(
            score=round(score, 4),
            passed=passed,
            faithfulness=round(faithfulness, 4),
            completeness=round(completeness, 4),
            grounded=grounded,
            missing_points=missing,
            feedback=feedback,
        )

    def _llm_judge(
        self, question: str, hits: list[RetrievedChunk], answer: str,
    ) -> dict | None:
        try:
            client = self._llm or LLMClient()
            return client.judge_answer_quality(question, hits, answer)
        except Exception:
            return None
