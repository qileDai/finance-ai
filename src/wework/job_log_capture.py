"""Capture src.* logging records during an ICRIS job for admin step logs."""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any


class JobLogCapture(logging.Handler):
    """Thread-safe buffer of structured log lines (src.* only)."""

    def __init__(self, *, max_entries: int = 3000) -> None:
        super().__init__(level=logging.INFO)
        self.max_entries = max(100, int(max_entries))
        self._lock = threading.Lock()
        self._entries: list[dict[str, Any]] = []
        self._dirty = 0

    def emit(self, record: logging.LogRecord) -> None:
        name = record.name or ""
        if not (name == "src" or name.startswith("src.")):
            return
        try:
            msg = self.format(record) if self.formatter else record.getMessage()
        except Exception:
            msg = record.getMessage()
        level = (record.levelname or "INFO").upper()
        if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            level = "INFO"
        if level == "DEBUG":
            return
        entry = {
            "level": level,
            "message": str(msg),
            "time": datetime.now().strftime("%H:%M:%S"),
        }
        with self._lock:
            self._entries.append(entry)
            if len(self._entries) > self.max_entries:
                overflow = len(self._entries) - self.max_entries
                del self._entries[:overflow]
            self._dirty += 1

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(x) for x in self._entries]

    def dirty_count(self) -> int:
        with self._lock:
            return self._dirty

    def mark_flushed(self) -> None:
        with self._lock:
            self._dirty = 0

    def merge_ctx_messages(self, messages: list[str] | None) -> None:
        """Append WorkflowContext.messages that are not already present."""
        if not messages:
            return
        with self._lock:
            existing = {str(e.get("message") or "") for e in self._entries}
            for raw in messages:
                text = str(raw or "").strip()
                if not text or text in existing:
                    continue
                self._entries.append(
                    {
                        "level": "INFO",
                        "message": text,
                        "time": datetime.now().strftime("%H:%M:%S"),
                    }
                )
                existing.add(text)
                self._dirty += 1
            if len(self._entries) > self.max_entries:
                overflow = len(self._entries) - self.max_entries
                del self._entries[:overflow]

    def append_error(self, message: str) -> None:
        text = str(message or "").strip()
        if not text:
            return
        with self._lock:
            self._entries.append(
                {
                    "level": "ERROR",
                    "message": text,
                    "time": datetime.now().strftime("%H:%M:%S"),
                }
            )
            self._dirty += 1
            if len(self._entries) > self.max_entries:
                del self._entries[0]

    def install(self, logger: logging.Logger | None = None) -> None:
        target = logger or logging.getLogger()
        target.addHandler(self)

    def uninstall(self, logger: logging.Logger | None = None) -> None:
        target = logger or logging.getLogger()
        try:
            target.removeHandler(self)
        except Exception:
            pass
