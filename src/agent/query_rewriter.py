"""低分检索时的查询改写"""

from __future__ import annotations

import re

from config.settings import settings
from src.agent.models import RetrievalEval
from src.rag.models import RetrievedChunk

CAUTION_QUERY_RE = re.compile(r"注意|要注意|注意事项")
DURATION_QUERY_RE = re.compile(r"多久|多长时间|要多久|多久能|周期")

# 多策略重试：核心业务关键词（优化 2 keyword_extract 策略）
_BUSINESS_KEYWORDS: tuple[str, ...] = (
    "香港公司注册", "香港银行开户", "商业登记证", "注册地址", "公司章程",
    "注意事项", "审核周期", "资料清单", "地址证明", "公司秘书",
    "NNC1", "NNC1A", "BR", "CR", "SCR",
    "注册", "开户", "香港", "内地", "银行", "资料", "材料",
    "审核", "周期", "费用", "流程", "公司", "董事", "股东",
    "护照", "身份证", "时间", "面签",
)

# 重试策略链顺序（优化 2）
RETRY_STRATEGIES: tuple[str, ...] = ("rewrite", "relax_scope", "keyword_extract")


class QueryRewriter:
    def __init__(self, llm=None) -> None:
        self._llm = llm

    def rewrite(
        self,
        question: str,
        hits: list[RetrievedChunk],
        eval_result: RetrievalEval,
    ) -> str:
        # 高置信未过线时仍优先规则改写，避免额外 LLM
        high_th = float(
            getattr(settings, "agent_high_confidence_skip_rewrite", 0.70) or 0.70
        )
        if eval_result.score >= high_th * 0.9:
            return self._rule_rewrite(question)
        if settings.agent_enable_llm_judge:
            llm_query = self._llm_rewrite(question, hits, eval_result)
            if llm_query:
                return llm_query
        return self._rule_rewrite(question)

    def rewrite_with_strategy(
        self,
        question: str,
        hits: list[RetrievedChunk],
        eval_result: RetrievalEval,
        strategy: str,
        *,
        scope: str | None = None,
    ) -> tuple[str, str | None]:
        """按指定策略改写查询（优化 2 多策略重试）。

        返回 (new_query, new_scope)；new_scope 为 None 表示沿用当前 scope。
        - rewrite：调用现有 rewrite 逻辑
        - relax_scope：不改写查询，将 scope 放宽到 all
        - keyword_extract：提取 2-4 个核心业务关键词
        """
        if strategy == "relax_scope":
            cur = (scope or "").strip().lower()
            # 当前限定 hk/cn 时扩大到 all；已是 all 则无变化
            if cur in ("", "hk", "cn"):
                return question, "all"
            return question, None
        if strategy == "keyword_extract":
            return self._keyword_extract(question), None
        # 默认 / "rewrite"
        return self.rewrite(question, hits, eval_result), None

    def _keyword_extract(self, question: str) -> str:
        """提取 2-4 个核心业务关键词作为新查询（优化 2）。

        优先匹配预置业务关键词；不足时回退到原问题。
        """
        found: list[str] = []
        for kw in _BUSINESS_KEYWORDS:
            if kw in question and kw not in found:
                found.append(kw)
            if len(found) >= 4:
                break
        if len(found) < 2:
            return question
        return " ".join(found[:4])

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

            client = self._llm or LLMClient()
            return client.rewrite_query(question, hits, eval_result.feedback)
        except Exception:
            return ""
