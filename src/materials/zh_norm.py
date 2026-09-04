"""简繁归一：仅用于标签匹配，不要拿去改姓名/地址原文。"""

from __future__ import annotations


def to_hans(text: str) -> str:
    """转简体，供关键字匹配。失败则原样返回。"""
    s = text or ""
    if not s:
        return ""
    try:
        import zhconv

        return zhconv.convert(s, "zh-cn")
    except Exception:
        return s
