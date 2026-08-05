"""检索质量打分"""

from __future__ import annotations

import re

from config.settings import settings
from src.agent.models import RetrievalEval
from src.llm.openai_client import LLMClient
from src.rag.models import RetrievedChunk

RRF_NORM = 0.05  # 典型 RRF 高分区间参考值


def _query_bigrams(text: str) -> list[str]:
    text = re.sub(r"\s+", "", text)
    if len(text) < 2:
        return [text] if text else []
    return [text[i : i + 2] for i in range(len(text) - 1)]


def _keyword_coverage(query: str, hits: list[RetrievedChunk]) -> float:
    bigrams = _query_bigrams(query)
    if not bigrams:
        return 0.0
    merged = "\n".join(h.text for h in hits[:3])
    hits_count = sum(1 for bg in bigrams if bg in merged)
    return hits_count / len(bigrams)


class RetrievalScorer:
    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm

    def score(self, question: str, hits: list[RetrievedChunk]) -> RetrievalEval:
        if not hits:
            return RetrievalEval(
                score=0.0,
                passed=False,
                feedback="未检索到任何知识片段，需改写查询或扩大检索范围。",
            )

        top_score = hits[0].score
        hit_count = len(hits)
        kw_cov = _keyword_coverage(question, hits)

        rrf_norm = min(top_score / RRF_NORM, 1.0) if top_score > 0 else 0.0
        hit_ratio = min(hit_count / max(settings.rag_top_k, 1), 1.0)

        # 高置信启发式：直接过线，跳过 LLM retrieval judge
        heuristic_score = 0.35 * rrf_norm + 0.15 * hit_ratio + 0.50 * kw_cov
        high_th = float(
            getattr(settings, "agent_high_confidence_skip_rewrite", 0.70) or 0.70
        )
        skip_llm = (
            heuristic_score >= high_th
            and not settings.agent_llm_judge_always
        )

        llm_relevance = 0.0
        if (
            not skip_llm
            and settings.agent_enable_llm_judge
            and (
                settings.agent_llm_judge_always
                or (rrf_norm * 0.25 + kw_cov * 0.25)
                < settings.agent_retrieval_llm_threshold
            )
        ):
            llm_relevance = self._llm_relevance(question, hits)

        if llm_relevance > 0:
            score = (
                0.25 * rrf_norm
                + 0.10 * hit_ratio
                + 0.25 * kw_cov
                + 0.40 * llm_relevance
            )
        else:
            score = heuristic_score

        passed = score >= settings.agent_retrieval_threshold
        if llm_relevance > 0 and llm_relevance < 0.6:
            passed = False

        feedback = ""
        if not passed:
            if kw_cov < 0.3:
                feedback = "检索片段与问题关键词匹配度低，建议补充业务关键词后重检索。"
            elif hit_count < 2:
                feedback = "命中片段过少，建议扩展查询词。"
            else:
                feedback = "检索相关性不足，建议改写查询。"

        return RetrievalEval(
            score=round(score, 4),
            passed=passed,
            top_score=top_score,
            hit_count=hit_count,
            keyword_coverage=round(kw_cov, 4),
            llm_relevance=round(llm_relevance, 4),
            feedback=feedback,
            details={
                "rrf_norm": round(rrf_norm, 4),
                "hit_ratio": round(hit_ratio, 4),
                "skipped_llm": skip_llm,
            },
        )

    def _llm_relevance(self, question: str, hits: list[RetrievedChunk]) -> float:
        try:
            client = self._llm or LLMClient()
            scores = client.judge_retrieval_relevance(question, hits[:3])
            if not scores:
                return 0.0
            return sum(scores) / len(scores) / 5.0
        except Exception:
            return 0.0
