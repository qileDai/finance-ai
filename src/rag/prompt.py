"""检索片段格式化"""

from __future__ import annotations

from src.rag.models import RetrievedChunk


def format_hits_for_prompt(hits: list[RetrievedChunk]) -> str:
    if not hits:
        return ""
    parts: list[str] = []
    for i, hit in enumerate(hits, start=1):
        parts.append(
            f"【检索片段 {i}】来源: {hit.source_path}\n{hit.text.strip()}"
        )
    return "\n\n".join(parts)
