"""高频 FAQ 精确/近义缓存：命中则跳过 RAG 与生成。"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from config.settings import PROJECT_ROOT, settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FaqHit:
    id: str
    answer: str
    source: str
    match_type: str  # exact | alias


def _normalize(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"\s+", "", t)
    for ch in ("？", "?", "。", "！", "!", "，", ",", "、"):
        t = t.replace(ch, "")
    return t


@lru_cache(maxsize=4)
def _load_faq_items(path_str: str, mtime: float) -> list[dict]:
    path = Path(path_str)
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("FAQ 加载失败 %s: %s", path, e)
        return []
    items = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return []
    return [it for it in items if isinstance(it, dict)]


def _faq_path() -> Path:
    p = Path((settings.agent_faq_path or "docs/knowledge/faq.json").strip())
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p


def lookup_faq(question: str) -> FaqHit | None:
    """规范化全等优先，其次最长 alias 子串命中。"""
    if not getattr(settings, "agent_faq_enabled", True):
        return None
    path = _faq_path()
    if not path.is_file():
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    items = _load_faq_items(str(path), mtime)
    q = _normalize(question)
    if not q or len(q) < 2:
        return None

    for it in items:
        for m in it.get("match") or []:
            if _normalize(str(m)) == q:
                ans = str(it.get("answer") or "").strip()
                if ans:
                    return FaqHit(
                        id=str(it.get("id") or ""),
                        answer=ans,
                        source=str(it.get("source") or ""),
                        match_type="exact",
                    )

    best: FaqHit | None = None
    best_len = 0
    for it in items:
        aliases = [str(a) for a in (it.get("aliases") or []) if str(a).strip()]
        for a in aliases:
            na = _normalize(a)
            if len(na) < 4:
                continue
            hit = (na in q) or (len(q) >= 4 and q in na)
            if not hit or len(na) < best_len:
                continue
            ans = str(it.get("answer") or "").strip()
            if not ans:
                continue
            best_len = len(na)
            best = FaqHit(
                id=str(it.get("id") or ""),
                answer=ans,
                source=str(it.get("source") or ""),
                match_type="alias",
            )
    return best
