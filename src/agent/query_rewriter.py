"""低分检索时的查询改写"""

from __future__ import annotations

import re

from config.settings import settings
from src.agent.models import RetrievalEval
from src.rag.models import RetrievedChunk

CAUTION_QUERY_RE = re.compile(r"注意|要注意|注意事项")
DURATION_QUERY_RE = re.compile(r"多久|多长时间|要多久|多久能|周期")


class QueryRewriter:
    def rewrite(
        self,
        question: str,
        hits: list[RetrievedChunk],
        eval_result: RetrievalEval,
    ) -> str:
        if settings.agent_enable_llm_judge:
            llm_query = self._llm_rewrite(question, hits, eval_result)
            if llm_query:
                return llm_query
        return self._rule_rewrite(question)

    def _rule_rewrite(self, question: str) -> str:
        extra: list[str] = []
        if CAUTION_QUERY_RE.search(question):
            extra.extend(["注意事项", "面签"])
        if "开户" in question:
            extra.append("香港银行开户")
        if "注册" in question:
            extra.append("香港公司注册")
        if "资料" in question or "材料" in question:
            extra.append("资料清单")
        if DURATION_QUERY_RE.search(question):
            extra.extend(["审核周期", "开户审核", "正式提交"])
        if not extra:
            return question
        return f"{question} {' '.join(extra)}"

    def _llm_rewrite(
        self,
        question: str,
        hits: list[RetrievedChunk],
        eval_result: RetrievalEval,
    ) -> str:
        try:
            from src.llm.openai_client import LLMClient

            return LLMClient().rewrite_query(question, hits, eval_result.feedback)
        except Exception:
            return ""
