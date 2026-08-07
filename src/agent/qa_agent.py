"""QA Agent 自我纠错循环 + 三级混合回答"""

from __future__ import annotations

import logging
import math
import time

from config.settings import settings
from src.agent.faq_cache import lookup_faq
from src.agent.models import (
    ABSTAIN_MESSAGE,
    HUMAN_MESSAGE,
    AgentAction,
    AgentTraceStep,
    AnswerMode,
    QAResult,
)
from src.agent.domain import (
    hits_contain_timeline_or_topic,
    hits_have_timeline_content,
    is_registration_domain,
)
from src.agent.query_rewriter import RETRY_STRATEGIES, QueryRewriter
from src.agent.scoring.answer_scorer import AnswerScorer
from src.agent.scoring.retrieval_scorer import RetrievalScorer
from src.llm.openai_client import LLMClient
from src.rag.hybrid_retriever import HybridRetriever
from src.rag.models import RetrievedChunk
from src.rag.prompt import format_hits_for_prompt

logger = logging.getLogger(__name__)


def _merge_hits(existing: list[RetrievedChunk], new: list[RetrievedChunk]) -> list[RetrievedChunk]:
    seen = {h.chunk_id for h in existing}
    merged = list(existing)
    for hit in new:
        if hit.chunk_id not in seen:
            seen.add(hit.chunk_id)
            merged.append(hit)
    merged.sort(key=lambda h: h.score, reverse=True)
    return merged


def _citations_from_hits(hits: list[RetrievedChunk]) -> list[str]:
    cites: list[str] = []
    seen: set[str] = set()
    for hit in hits[:5]:
        label = hit.step_title or hit.source_path
        if label and label not in seen:
            seen.add(label)
            cites.append(label)
    return cites


def _looks_empty_talk(answer: str) -> bool:
    """轻量空话检测：过短或仅套话。"""
    t = (answer or "").strip()
    if len(t) < 8:
        return True
    hollow = ("抱歉", "无法回答", "不清楚", "不知道", "建议咨询", "请联系人工")
    if len(t) < 24 and any(h in t for h in hollow):
        return True
    return False


def _elapsed_ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """向量余弦相似度（优化 4 一致性检查用）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class QAAgent:
    def __init__(
        self,
        *,
        retriever: HybridRetriever | None = None,
        llm: LLMClient | None = None,
        retrieval_scorer: RetrievalScorer | None = None,
        answer_scorer: AnswerScorer | None = None,
        query_rewriter: QueryRewriter | None = None,
        store=None,
    ) -> None:
        self.llm = llm or LLMClient()
        self.retriever = retriever or HybridRetriever()
        self.retrieval_scorer = retrieval_scorer or RetrievalScorer(llm=self.llm)
        self.answer_scorer = answer_scorer or AnswerScorer(llm=self.llm)
        self.query_rewriter = query_rewriter or QueryRewriter(llm=self.llm)
        # ExternalGroupStore 实例（可选，用于一致性检查优化 4）
        self._store = store
        # Embedder 懒加载（一致性检查优化 4）
        self._embedder = None
        self._embedder_unavailable = False

    def _resolve_failure_action(self) -> AgentAction:
        if settings.agent_escalate_to_human:
            return AgentAction.HUMAN
        if settings.agent_silent_on_no_answer or not settings.agent_abstain_message_to_customer:
            return AgentAction.SILENT
        return AgentAction.ABSTAIN

    def _failure_answer(self, action: AgentAction) -> str:
        if action == AgentAction.HUMAN:
            return HUMAN_MESSAGE
        if action == AgentAction.ABSTAIN:
            msg = (settings.agent_abstain_message or "").strip()
            return msg or ABSTAIN_MESSAGE
        return ""

    def _enforce_response_length(
        self,
        question: str,
        answer: str,
        *,
        trace: list[AgentTraceStep] | None = None,
    ) -> str:
        """超长回答自适应压缩（优化 5）。

        先尝试 LLM 摘要，仍超长则 UTF-8 安全截断。短回答原样返回。
        """
        max_bytes = int(getattr(settings, "agent_response_max_bytes", 1800) or 0)
        if max_bytes <= 0 or not answer:
            return answer
        original_bytes = len(answer.encode("utf-8"))
        if original_bytes <= max_bytes:
            return answer
        action = "none"
        summarize = bool(
            getattr(settings, "agent_response_summarize_enabled", True)
        )
        if summarize:
            # UTF-8 中文约 3 字节/字符，估算目标字符数
            target_chars = max(40, max_bytes // 3)
            summarized = self.llm.summarize_answer(question, answer, target_chars)
            if summarized and summarized != answer:
                answer = summarized
                action = "summarize"
        if len(answer.encode("utf-8")) > max_bytes:
            answer = self._truncate_utf8(answer, max_bytes)
            action = (
                f"{action}+truncate" if action != "none" else "truncate"
            )
        if trace is not None:
            trace.append(
                AgentTraceStep(
                    step="enforce_length",
                    attempt=0,
                    data={
                        "original_bytes": original_bytes,
                        "final_bytes": len(answer.encode("utf-8")),
                        "max_bytes": max_bytes,
                        "action": action,
                    },
                )
            )
        return answer

    @staticmethod
    def _truncate_utf8(text: str, max_bytes: int) -> str:
        """UTF-8 安全截断：在句号/换行处截断并追加提示（优化 5）。"""
        if max_bytes <= 0 or len(text.encode("utf-8")) <= max_bytes:
            return text
        hint = "…（内容较长已截断，详情可回复「转人工」）"
        hint_bytes = len(hint.encode("utf-8"))
        budget = max(0, max_bytes - hint_bytes)
        truncated = text.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
        # 在最近的句号/换行处截断，避免半句
        for sep in ("\n", "。", "；", "！", "，"):
            idx = truncated.rfind(sep)
            if idx >= budget // 2:
                truncated = truncated[: idx + len(sep)]
                break
        return f"{truncated}{hint}"

    def _get_embedder(self):
        """懒加载 Embedder 用于一致性检查（优化 4）。不可用返回 None。"""
        if self._embedder_unavailable:
            return None
        if self._embedder is None:
            try:
                from src.rag.embedder import Embedder

                self._embedder = Embedder()
            except Exception as e:
                logger.debug("Embedder 不可用，一致性检查跳过: %s", e)
                self._embedder_unavailable = True
                return None
        return self._embedder

    def _check_consistency(
        self, question: str, answer: str, roomid: str
    ) -> tuple[bool, str]:
        """检测当前回答与历史相似问题的回答是否矛盾（优化 4）。

        返回 (is_contradictory, reason)。任何依赖不可用时返回 (False, "")。
        """
        if not roomid or not answer:
            return False, ""
        if not getattr(settings, "agent_consistency_check_enabled", True):
            return False, ""
        if self._store is None:
            return False, ""
        embedder = self._get_embedder()
        if embedder is None:
            return False, ""
        threshold = float(
            getattr(settings, "agent_consistency_similarity_threshold", 0.85) or 0.85
        )
        history_limit = int(
            getattr(settings, "agent_consistency_history_limit", 20) or 20
        )
        try:
            runs = self._store.get_recent_agent_runs(roomid, limit=history_limit)
        except Exception as e:
            logger.debug("获取历史 agent_runs 失败: %s", e)
            return False, ""
        if not runs:
            return False, ""
        hist_questions = [r.get("question", "") for r in runs]
        try:
            vecs = embedder.embed_texts([question] + hist_questions)
        except Exception as e:
            logger.debug("一致性检查 embedding 失败: %s", e)
            return False, ""
        if len(vecs) < len(runs) + 1:
            return False, ""
        cur_vec = vecs[0]
        best_idx = -1
        best_sim = 0.0
        for i, hv in enumerate(vecs[1:]):
            sim = _cosine_similarity(cur_vec, hv)
            if sim > best_sim:
                best_sim = sim
                best_idx = i
        if best_idx < 0 or best_sim < threshold:
            return False, ""
        hist = runs[best_idx]
        hist_q = hist.get("question", "")
        hist_a = hist.get("final_answer", "")
        if not hist_a:
            return False, ""
        contradictory = self.llm.check_answer_contradiction(
            question, answer, hist_q, hist_a
        )
        if not contradictory:
            return False, ""
        reason = f"与历史相似问题(sim={best_sim:.2f})回答矛盾"
        return True, reason

    def _domain_abstain_or_silent(
        self,
        *,
        run_id: str,
        question: str,
        hits: list[RetrievedChunk],
        retrieval_score: float,
        trace: list[AgentTraceStep],
        total_retries: int,
        reason: str,
        t_start: float | None = None,
    ) -> QAResult:
        """注册域失败优先固定兜底；非注册域可静默。"""
        if t_start is not None:
            trace.append(
                AgentTraceStep(
                    step="total",
                    attempt=0,
                    data={"elapsed_ms": _elapsed_ms(t_start)},
                )
            )
        if is_registration_domain(question) and not (
            settings.agent_silent_on_no_answer
            or not settings.agent_abstain_message_to_customer
        ):
            action = self._resolve_failure_action()
            if action != AgentAction.SILENT:
                answer = self._failure_answer(action)
                return QAResult(
                    run_id=run_id,
                    question=question,
                    answer=answer,
                    action=action,
                    confidence=0.0,
                    retrieval_score=round(retrieval_score, 4),
                    answer_score=0.0,
                    retries=total_retries,
                    answer_mode=AnswerMode.SILENT
                    if action == AgentAction.SILENT
                    else AnswerMode.CONTEXTUAL,
                    hits=hits,
                    citations=_citations_from_hits(hits),
                    trace=trace,
                    needs_human=action == AgentAction.HUMAN,
                    silent_reason=reason,
                )
        return self._silent_result(
            run_id=run_id,
            question=question,
            hits=hits,
            retrieval_score=retrieval_score,
            trace=trace,
            total_retries=total_retries,
            reason=reason,
        )

    def run(
        self,
        question: str,
        *,
        scope: str | None = None,
        roomid: str = "",
        history: list[str] | None = None,
        group_meta: dict[str, str] | None = None,
    ) -> QAResult:
        scope = (scope or settings.rag_scope or "hk").strip().lower()
        run_id = QAResult.new_run_id()
        trace: list[AgentTraceStep] = []
        total_retries = 0
        max_retries = settings.agent_max_retries
        history = history or []
        t_start = time.monotonic()

        # FAQ 快路径：跳过 RAG/生成
        t_faq = time.monotonic()
        faq = lookup_faq(question)
        if faq:
            cites = [faq.source] if faq.source and faq.source != "system" else []
            trace.append(
                AgentTraceStep(
                    step="faq",
                    attempt=0,
                    data={
                        "id": faq.id,
                        "match_type": faq.match_type,
                        "elapsed_ms": _elapsed_ms(t_faq),
                    },
                )
            )
            trace.append(
                AgentTraceStep(
                    step="total",
                    attempt=0,
                    data={"elapsed_ms": _elapsed_ms(t_start), "path": "faq"},
                )
            )
            return QAResult(
                run_id=run_id,
                question=question,
                answer=faq.answer,
                action=AgentAction.REPLY,
                confidence=0.95,
                retrieval_score=1.0,
                answer_score=0.95,
                retries=0,
                answer_mode=AnswerMode.KNOWLEDGE,
                hits=[],
                citations=cites,
                trace=trace,
            )

        query = question.strip()
        hits: list[RetrievedChunk] = []
        retrieval_score = 0.0
        r_eval = None
        skip_rewrite_th = float(
            getattr(settings, "agent_high_confidence_skip_rewrite", 0.70) or 0.70
        )
        # 多策略重试链（优化 2）
        multi_strategy = bool(
            getattr(settings, "agent_multi_strategy_retry_enabled", True)
        )

        for attempt in range(max_retries + 1):
            t_ret = time.monotonic()
            new_hits = self.retriever.retrieve(
                query, top_k=settings.rag_top_k, scope=scope,
            )
            hits = _merge_hits(hits, new_hits) if hits else new_hits
            r_eval = self.retrieval_scorer.score(question, hits)
            retrieval_score = r_eval.score
            high_conf = r_eval.passed and retrieval_score >= skip_rewrite_th
            trace.append(
                AgentTraceStep(
                    step="retrieve",
                    attempt=attempt,
                    data={
                        "query": query,
                        "scope": scope,
                        "score": r_eval.score,
                        "passed": r_eval.passed,
                        "hit_count": r_eval.hit_count,
                        "keyword_coverage": r_eval.keyword_coverage,
                        "feedback": r_eval.feedback,
                        "high_confidence": high_conf,
                        "elapsed_ms": _elapsed_ms(t_ret),
                    },
                )
            )
            if r_eval.passed or attempt >= max_retries:
                break
            if high_conf:
                break
            t_rw = time.monotonic()
            if multi_strategy and attempt < len(RETRY_STRATEGIES):
                strategy = RETRY_STRATEGIES[attempt]
                new_query, new_scope = self.query_rewriter.rewrite_with_strategy(
                    question, hits, r_eval, strategy, scope=scope,
                )
                if new_scope:
                    scope = new_scope
                query = new_query
            else:
                strategy = "rewrite"
                query = self.query_rewriter.rewrite(question, hits, r_eval)
            total_retries += 1
            trace.append(
                AgentTraceStep(
                    step="rewrite",
                    attempt=attempt,
                    data={
                        "query": query,
                        "strategy": strategy,
                        "scope": scope,
                        "elapsed_ms": _elapsed_ms(t_rw),
                    },
                )
            )

        use_knowledge = bool(hits) and r_eval is not None and r_eval.passed
        soft_min = float(settings.agent_soft_knowledge_min_score or 0.0)
        use_soft_knowledge = (
            not use_knowledge
            and hits
            and is_registration_domain(question)
            and retrieval_score >= soft_min
            and hits_contain_timeline_or_topic(hits, question)
        )

        if use_knowledge or use_soft_knowledge:
            result = self._run_knowledge_mode(
                run_id=run_id,
                question=question,
                hits=hits,
                retrieval_score=retrieval_score,
                trace=trace,
                total_retries=total_retries,
                max_retries=max_retries,
                soft=use_soft_knowledge,
                history=history,
                group_meta=group_meta or {},
                roomid=roomid,
                skip_llm_judge=retrieval_score
                >= float(getattr(settings, "agent_high_confidence_skip_judge", 0.75) or 0.75),
            )
            result.trace.append(
                AgentTraceStep(
                    step="total",
                    attempt=0,
                    data={
                        "elapsed_ms": _elapsed_ms(t_start),
                        "path": "soft" if use_soft_knowledge else "knowledge",
                    },
                )
            )
            return result

        if settings.agent_contextual_fallback:
            result = self._run_contextual_mode(
                run_id=run_id,
                question=question,
                hits=hits,
                retrieval_score=retrieval_score,
                trace=trace,
                total_retries=total_retries,
                history=history,
                group_meta=group_meta or {},
                roomid=roomid,
            )
            result.trace.append(
                AgentTraceStep(
                    step="total",
                    attempt=0,
                    data={"elapsed_ms": _elapsed_ms(t_start), "path": "contextual"},
                )
            )
            return result

        return self._domain_abstain_or_silent(
            run_id=run_id,
            question=question,
            hits=hits,
            retrieval_score=retrieval_score,
            trace=trace,
            total_retries=total_retries,
            reason="无有效检索且未启用上下文兜底",
            t_start=t_start,
        )

    def _run_knowledge_mode(
        self,
        *,
        run_id: str,
        question: str,
        hits: list[RetrievedChunk],
        retrieval_score: float,
        trace: list[AgentTraceStep],
        total_retries: int,
        max_retries: int,
        soft: bool = False,
        history: list[str] | None = None,
        group_meta: dict[str, str] | None = None,
        roomid: str = "",
        skip_llm_judge: bool = False,
    ) -> QAResult:
        context = format_hits_for_prompt(hits)
        history = history or []
        group_meta = group_meta or {}
        t_gen = time.monotonic()
        answer = self.llm.generate_answer(
            question,
            context,
            history=history,
            group_meta=group_meta,
        )
        trace.append(
            AgentTraceStep(
                step="generate",
                attempt=0,
                data={"elapsed_ms": _elapsed_ms(t_gen), "soft": soft},
            )
        )
        answer_score = 0.0
        final_action = AgentAction.REPLY
        answer_mode = AnswerMode.KNOWLEDGE

        for attempt in range(max_retries + 1):
            t_sc = time.monotonic()
            a_eval = self.answer_scorer.score(
                question,
                hits,
                answer,
                skip_llm_judge=skip_llm_judge and attempt == 0,
            )
            answer_score = a_eval.score
            # 首轮启发式过线且高置信：确认跳过了 LLM
            if (
                skip_llm_judge
                and attempt == 0
                and a_eval.passed
                and not settings.agent_llm_judge_always
            ):
                skip_llm_judge = True
            trace.append(
                AgentTraceStep(
                    step="answer",
                    attempt=attempt,
                    data={
                        "mode": answer_mode.value,
                        "soft": soft,
                        "score": a_eval.score,
                        "passed": a_eval.passed,
                        "faithfulness": a_eval.faithfulness,
                        "completeness": a_eval.completeness,
                        "grounded": a_eval.grounded,
                        "feedback": a_eval.feedback,
                        "skip_llm_judge": skip_llm_judge and attempt == 0,
                        "history_len": len(history),
                        "has_materials_summary": bool(group_meta.get("materials_summary")),
                        "elapsed_ms": _elapsed_ms(t_sc),
                    },
                )
            )
            if a_eval.passed or attempt >= max_retries:
                if not a_eval.passed and settings.agent_abstain_on_low_confidence:
                    if soft and settings.agent_contextual_fallback:
                        return self._run_contextual_mode(
                            run_id=run_id,
                            question=question,
                            hits=hits,
                            retrieval_score=retrieval_score,
                            trace=trace,
                            total_retries=total_retries,
                            history=history,
                            group_meta=group_meta,
                            roomid=roomid,
                            allow_soft_fallback=False,
                        )
                    final_action = self._resolve_failure_action()
                    if (
                        final_action == AgentAction.SILENT
                        and is_registration_domain(question)
                    ):
                        final_action = AgentAction.ABSTAIN
                    answer = self._failure_answer(final_action)
                    if final_action == AgentAction.SILENT:
                        answer_mode = AnswerMode.SILENT
                break
            t_rg = time.monotonic()
            answer = self.llm.regenerate_answer(
                question, context, answer, a_eval.feedback,
            )
            total_retries += 1
            trace.append(
                AgentTraceStep(
                    step="regenerate",
                    attempt=attempt,
                    data={"elapsed_ms": _elapsed_ms(t_rg)},
                )
            )

        confidence = min(retrieval_score, answer_score)
        # 回答一致性检查（优化 4）：矛盾时追加免责声明
        if final_action == AgentAction.REPLY and answer:
            contradictory, c_reason = self._check_consistency(
                question, answer, roomid
            )
            if contradictory:
                trace.append(
                    AgentTraceStep(
                        step="consistency",
                        attempt=0,
                        data={"contradictory": True, "reason": c_reason},
                    )
                )
                if getattr(
                    settings, "agent_consistency_append_disclaimer", True
                ):
                    disclaimer = (
                        "（注：本次回答与此前略有差异，具体以专员核实为准；"
                        "也可回复「转人工」。）"
                    )
                    if disclaimer not in answer:
                        answer = f"{answer}\n{disclaimer}"
        # 自适应长度控制（优化 5）
        answer = self._enforce_response_length(question, answer, trace=trace)
        return QAResult(
            run_id=run_id,
            question=question,
            answer=answer,
            action=final_action,
            confidence=round(confidence, 4),
            retrieval_score=round(retrieval_score, 4),
            answer_score=round(answer_score, 4),
            retries=total_retries,
            answer_mode=answer_mode,
            hits=hits,
            citations=_citations_from_hits(hits),
            trace=trace,
            needs_human=final_action == AgentAction.HUMAN,
            silent_reason="知识库回答质量不足" if final_action == AgentAction.SILENT else "",
        )

    def _run_contextual_mode(
        self,
        *,
        run_id: str,
        question: str,
        hits: list[RetrievedChunk],
        retrieval_score: float,
        trace: list[AgentTraceStep],
        total_retries: int,
        history: list[str],
        group_meta: dict[str, str],
        roomid: str = "",
        allow_soft_fallback: bool = True,
    ) -> QAResult:
        t_ctx = time.monotonic()
        ctx_result = self.llm.generate_contextual_answer(
            question, history=history, group_meta=group_meta, hits=hits,
        )
        trace.append(
            AgentTraceStep(
                step="contextual",
                attempt=0,
                data={
                    "can_answer": ctx_result.get("can_answer"),
                    "reason": ctx_result.get("reason", ""),
                    "elapsed_ms": _elapsed_ms(t_ctx),
                },
            )
        )

        if not ctx_result.get("can_answer"):
            soft_min = float(settings.agent_soft_knowledge_min_score or 0.0)
            if (
                allow_soft_fallback
                and is_registration_domain(question)
                and hits
                and retrieval_score >= soft_min
                and hits_have_timeline_content(hits)
            ):
                return self._run_knowledge_mode(
                    run_id=run_id,
                    question=question,
                    hits=hits,
                    retrieval_score=retrieval_score,
                    trace=trace,
                    total_retries=total_retries,
                    max_retries=0,
                    soft=True,
                    roomid=roomid,
                )
            return self._domain_abstain_or_silent(
                run_id=run_id,
                question=question,
                hits=hits,
                retrieval_score=retrieval_score,
                trace=trace,
                total_retries=total_retries,
                reason=ctx_result.get("reason") or "LLM 判定无法回答",
            )

        answer = str(ctx_result.get("answer") or "").strip()
        # 弱证据路径强制附免责声明
        disclaimer = "（以上供参考，具体以专员核实为准；也可回复「转人工」。）"
        if answer and disclaimer not in answer:
            answer = f"{answer}\n{disclaimer}"
        if not answer or _looks_empty_talk(answer):
            return self._domain_abstain_or_silent(
                run_id=run_id,
                question=question,
                hits=hits,
                retrieval_score=retrieval_score,
                trace=trace,
                total_retries=total_retries,
                reason="LLM 返回空回答或空话",
            )

        answer_score = 0.75
        if hits:
            a_eval = self.answer_scorer.score(question, hits, answer)
            answer_score = a_eval.score
            trace.append(
                AgentTraceStep(
                    step="contextual_answer_score",
                    attempt=0,
                    data={
                        "score": a_eval.score,
                        "passed": a_eval.passed,
                        "faithfulness": a_eval.faithfulness,
                        "completeness": a_eval.completeness,
                        "feedback": a_eval.feedback,
                    },
                )
            )
            if not a_eval.passed and settings.agent_abstain_on_low_confidence:
                return self._domain_abstain_or_silent(
                    run_id=run_id,
                    question=question,
                    hits=hits,
                    retrieval_score=retrieval_score,
                    trace=trace,
                    total_retries=total_retries,
                    reason="上下文回答质检未通过",
                )

        # 自适应长度控制（优化 5）
        answer = self._enforce_response_length(question, answer, trace=trace)
        return QAResult(
            run_id=run_id,
            question=question,
            answer=answer,
            action=AgentAction.REPLY,
            confidence=round(max(retrieval_score, min(answer_score, 0.9)), 4),
            retrieval_score=round(retrieval_score, 4),
            answer_score=round(answer_score, 4),
            retries=total_retries,
            answer_mode=AnswerMode.CONTEXTUAL,
            hits=hits,
            citations=_citations_from_hits(hits),
            trace=trace,
        )

    def _silent_result(
        self,
        *,
        run_id: str,
        question: str,
        hits: list[RetrievedChunk],
        retrieval_score: float,
        trace: list[AgentTraceStep],
        total_retries: int,
        reason: str,
    ) -> QAResult:
        logger.info("QA 静默跳过: %s — %s", question[:60], reason)
        return QAResult(
            run_id=run_id,
            question=question,
            answer="",
            action=AgentAction.SILENT,
            confidence=0.0,
            retrieval_score=round(retrieval_score, 4),
            answer_score=0.0,
            retries=total_retries,
            answer_mode=AnswerMode.SILENT,
            hits=hits,
            citations=_citations_from_hits(hits),
            trace=trace,
            silent_reason=reason,
        )
