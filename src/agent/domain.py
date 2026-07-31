"""注册域问题判定"""

from __future__ import annotations

import re

from src.rag.models import RetrievedChunk

REGISTRATION_DOMAIN_RE = re.compile(
    r"开户|注册|董事|股东|面签|年审|资料|材料|多久|周期|ICRIS|香港公司|银行"
)
TIMELINE_MARKERS = ("审核周期", "3-4 周", "3-4周", "工作日", "耐心等待", "正式提交")


def is_registration_domain(question: str) -> bool:
    return bool(REGISTRATION_DOMAIN_RE.search(question))


def hits_contain_timeline_or_topic(
    hits: list[RetrievedChunk],
    question: str,
) -> bool:
    if not hits:
        return False
    merged = "\n".join(h.text for h in hits[:3])
    step_titles = " ".join(h.step_title for h in hits[:3])
    combined = f"{merged}\n{step_titles}"

    if re.search(r"多久|周期|多长时间", question):
        if any(marker in combined for marker in TIMELINE_MARKERS):
            return True
    if "开户" in question and ("开户" in combined or "银行" in combined):
        return True
    if "注册" in question and "注册" in combined:
        return True
    if "面签" in question and "面签" in combined:
        return True
    return is_registration_domain(question)


def hits_have_timeline_content(hits: list[RetrievedChunk]) -> bool:
    merged = "\n".join(h.text for h in hits[:3])
    return any(marker in merged for marker in TIMELINE_MARKERS) or "周" in merged
