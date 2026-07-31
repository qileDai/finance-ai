"""QA Agent 自我纠错循环 + 三级混合回答"""

from __future__ import annotations

import logging

from config.settings import settings
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
from src.agent.query_rewriter import QueryRewriter
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


class QAAgent:
    def __init__(
        self,
        *,
        retriever: HybridRetriever | None = None,
        llm: LLMClient | None = None,
        retrieval_scorer: RetrievalScorer | None = None,
        answer_scorer: AnswerScorer | None = None,
        query_rewriter: QueryRewriter | None = None,
    ) -> None:
        self.retriever = retriever or HybridRetriever()
        self.llm = llm or LLMClient()
        self.retrieval_scorer = retrieval_scorer or RetrievalScorer()
        self.answer_scorer = answer_scorer or AnswerScorer()
        self.query_rewriter = query_rewriter or QueryRewriter()

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
            return ABSTAIN_MESSAGE
        return ""

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

        query = question.strip()
        hits: list[RetrievedChunk] = []
        retrieval_score = 0.0
        r_eval = None

        for attempt in range(max_retries + 1):
            new_hits = self.retriever.retrieve(
                query, top_k=settings.rag_top_k, scope=scope,
            )
            hits = _merge_hits(hits, new_hits) if hits else new_hits
            r_eval = self.retrieval_scorer.score(question, hits)
            retrieval_score = r_eval.score
            trace.append(
                AgentTraceStep(
                    step="retrieve",
                    attempt=attempt,
                    data={
                        "query": query,
                        "score": r_eval.score,
                        "passed": r_eval.passed,
                        "hit_count": r_eval.hit_count,
                        "keyword_coverage": r_eval.keyword_coverage,
                        "feedback": r_eval.feedback,
                    },
                )
            )
            if r_eval.passed or attempt >= max_retries:
                break
            query = self.query_rewriter.rewrite(question, hits, r_eval)
            total_retries += 1

        use_knowledge = bool(hits) and r_eval is not None and r_eval.passed
        use_soft_knowledge = (
            not use_knowledge
            and hits
            and is_registration_domain(question)
            and hits_contain_timeline_or_topic(hits, question)
        )

        if use_knowledge or use_soft_knowledge:
            return self._run_knowledge_mode(
                run_id=run_id,
                question=question,
                hits=hits,
                retrieval_score=retrieval_score,
                trace=trace,
                total_retries=total_retries,
                max_retries=max_retries,
                soft=use_soft_knowledge,
                skip_low_confidence_silent=is_registration_domain(question),
            )

        if settings.agent_contextual_fallback:
            return self._run_contextual_mode(
                run_id=run_id,
                question=question,
                hits=hits,
                retrieval_score=retrieval_score,
                trace=trace,
                total_retries=total_retries,
                history=history,
                group_meta=group_meta or {},
            )

        return self._silent_result(
            run_id=run_id,
            question=question,
            hits=hits,
            retrieval_score=retrieval_score,
            trace=trace,
            total_retries=total_retries,
            reason="无有效检索且未启用上下文兜底",
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
        skip_low_confidence_silent: bool = False,
    ) -> QAResult:
        context = format_hits_for_prompt(hits)
        answer = self.llm.generate_answer(question, context)
        answer_score = 0.0
        final_action = AgentAction.REPLY
        answer_mode = AnswerMode.KNOWLEDGE

        for attempt in range(max_retries + 1):
            a_eval = self.answer_scorer.score(question, hits, answer)
            answer_score = a_eval.score
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
                    },
                )
            )
            if a_eval.passed or attempt >= max_retries:
                if (
                    not a_eval.passed
                    and settings.agent_abstain_on_low_confidence
                    and not soft
                    and not skip_low_confidence_silent
                ):
                    final_action = self._resolve_failure_action()
                    answer = self._failure_answer(final_action)
                    if final_action == AgentAction.SILENT:
                        answer_mode = AnswerMode.SILENT
                break
            answer = self.llm.regenerate_answer(
                question, context, answer, a_eval.feedback,
            )
            total_retries += 1

        confidence = min(retrieval_score, answer_score)
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
    ) -> QAResult:
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
                },
            )
        )

        if not ctx_result.get("can_answer"):
            if (
                is_registration_domain(question)
                and hits
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
                    skip_low_confidence_silent=True,
                )
            return self._silent_result(
                run_id=run_id,
                question=question,
                hits=hits,
                retrieval_score=retrieval_score,
                trace=trace,
                total_retries=total_retries,
                reason=ctx_result.get("reason") or "LLM 判定无法回答",
            )

        answer = str(ctx_result.get("answer") or "").strip()
        if not answer:
            return self._silent_result(
                run_id=run_id,
                question=question,
                hits=hits,
                retrieval_score=retrieval_score,
                trace=trace,
                total_retries=total_retries,
                reason="LLM 返回空回答",
            )

        return QAResult(
            run_id=run_id,
            question=question,
            answer=answer,
            action=AgentAction.REPLY,
            confidence=round(max(retrieval_score, 0.5), 4),
            retrieval_score=round(retrieval_score, 4),
            answer_score=0.75,
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
