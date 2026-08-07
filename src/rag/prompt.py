"""检索片段格式化"""

from __future__ import annotations

from src.rag.models import RetrievedChunk

# chunk_kind → 中文类型标签（script 为默认，不展示）
_CHUNK_KIND_LABEL: dict[str, str] = {
    "caution": "注意事项",
    "duration": "时效信息",
    "requirement": "要求",
}

# region → 中文地区标签
_REGION_LABEL: dict[str, str] = {
    "hk": "香港",
    "cn": "内地",
    "mainland": "内地",
}


def format_hits_for_prompt(hits: list[RetrievedChunk]) -> str:
    if not hits:
        return ""
    parts: list[str] = []
    for i, hit in enumerate(hits, start=1):
        meta_segments: list[str] = [f"来源: {hit.source_path}"]
        if hit.step_title:
            meta_segments.append(f"步骤: {hit.step_title}")
        region_label = _REGION_LABEL.get((hit.region or "").strip().lower())
        if region_label:
            meta_segments.append(f"地区: {region_label}")
        kind_label = _CHUNK_KIND_LABEL.get((hit.chunk_kind or "").strip().lower())
        if kind_label:
            meta_segments.append(f"类型: {kind_label}")
        meta_line = " | ".join(meta_segments)
        parts.append(f"【检索片段 {i}】{meta_line}\n{hit.text.strip()}")
    return "\n\n".join(parts)
