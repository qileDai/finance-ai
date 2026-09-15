"""ICRIS Worker 观测心跳：独立 JSON 文件，不写任务库、不持 CDP 锁。"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.settings import PROJECT_ROOT

logger = logging.getLogger(__name__)

HEARTBEAT_PATH = PROJECT_ROOT / "data" / "icris-worker.heartbeat.json"
_STALE_AFTER_S = 120.0


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def touch_worker_heartbeat(loop: str = "register") -> None:
    """bot 各循环旁路刷新心跳。失败忽略，不影响过单。"""
    name = str(loop or "register").strip() or "register"
    now = time.time()
    payload: dict[str, Any] = {}
    try:
        if HEARTBEAT_PATH.is_file():
            raw = json.loads(HEARTBEAT_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                payload = raw
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    payload["ts"] = now
    payload["updated_at"] = _utc_iso()
    payload["pid"] = os.getpid()
    payload[name] = now
    try:
        HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_PATH.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        logger.debug("Worker 心跳写入失败", exc_info=True)


def read_worker_heartbeat(*, stale_after_s: float | None = None) -> dict[str, Any]:
    """Admin 只读心跳。文件不存在视为无心跳。"""
    limit = float(stale_after_s if stale_after_s is not None else _STALE_AFTER_S)
    empty = {
        "present": False,
        "stale": True,
        "age_seconds": None,
        "updated_at": "",
        "pid": 0,
        "label": "无心跳",
    }
    if not HEARTBEAT_PATH.is_file():
        return empty
    try:
        raw = json.loads(HEARTBEAT_PATH.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return empty
    if not isinstance(raw, dict):
        return empty
    try:
        ts = float(raw.get("ts") or 0)
    except (TypeError, ValueError):
        ts = 0.0
    if ts <= 0:
        return empty
    age = max(0.0, time.time() - ts)
    stale = age > max(1.0, limit)
    return {
        "present": True,
        "stale": stale,
        "age_seconds": round(age, 1),
        "updated_at": str(raw.get("updated_at") or ""),
        "pid": int(raw.get("pid") or 0),
        "label": "异常" if stale else "正常",
    }
