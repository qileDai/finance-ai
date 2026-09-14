"""Capture src.* logging records during an ICRIS job for admin step logs."""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Iterable

ACTIVATION_LOG_BANNER = "—— 账号激活开始 ——"
NNC1_LOG_BANNER = "—— NNC1 填表开始 ——"

_PHASES = frozenset({"register", "activation", "nnc1"})

ACTIVATION_LOG_PREFIXES: tuple[str, ...] = (
    "src.browser.icris_activation",
    "src.wework.icris_activation_worker",
    "src.email.",
)

NNC1_LOG_PREFIXES: tuple[str, ...] = (
    "src.browser.icris_nnc1_form",
    "src.browser.icris_login",
    "src.browser.icris_ui_common",
    "src.browser.cdp_",
    "src.browser.launcher",
    "src.browser.stealth",
    "src.browser.captcha",
    "src.browser.icris_captcha",
    "src.wework.icris_form_notify",
    "src.wework.icris_activation_worker",
)


class JobLogCapture(logging.Handler):
    """Thread-safe buffer of structured log lines (src.* only)."""

    def __init__(
        self,
        *,
        max_entries: int = 3000,
        name_prefixes: Iterable[str] | None = None,
        phase: str = "",
    ) -> None:
        super().__init__(level=logging.INFO)
        self.max_entries = max(100, int(max_entries))
        self._name_prefixes = tuple(p for p in (name_prefixes or ()) if p)
        p = str(phase or "").strip()
        self._phase = p if p in _PHASES else ""
        self._lock = threading.Lock()
        self._entries: list[dict[str, Any]] = []
        self._dirty = 0

    def _name_ok(self, name: str) -> bool:
        if not (name == "src" or name.startswith("src.")):
            return False
        if not self._name_prefixes:
            return True
        return any(name == p or name.startswith(p) for p in self._name_prefixes)

    def emit(self, record: logging.LogRecord) -> None:
        name = record.name or ""
        if not self._name_ok(name):
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
        self._append_entry(level, str(msg))

    def _entry(self, level: str, message: str) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "level": level,
            "message": message,
            "time": datetime.now().strftime("%H:%M:%S"),
        }
        if self._phase:
            entry["phase"] = self._phase
        return entry

    def _append_entry(self, level: str, message: str) -> None:
        entry = self._entry(level, message)
        with self._lock:
            self._entries.append(entry)
            if len(self._entries) > self.max_entries:
                overflow = len(self._entries) - self.max_entries
                del self._entries[:overflow]
            self._dirty += 1

    def append_info(self, message: str) -> None:
        text = str(message or "").strip()
        if not text:
            return
        self._append_entry("INFO", text)

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
                self._entries.append(self._entry("INFO", text))
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
            self._entries.append(self._entry("ERROR", text))
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


class JobLogSession:
    """阶段日志：保留已有 result_messages，本阶段 snapshot 追加写回。"""

    def __init__(
        self,
        store: Any,
        job_id: int,
        *,
        name_prefixes: Iterable[str] | None = None,
        banner: str = "",
        flush_interval: float = 5.0,
        phase: str = "",
    ) -> None:
        self.store = store
        self.job_id = int(job_id)
        self.capture = JobLogCapture(name_prefixes=name_prefixes, phase=phase)
        self._banner = str(banner or "").strip()
        self._flush_interval = max(0.5, float(flush_interval or 5.0))
        self._prefix: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> JobLogSession:
        try:
            raw = self.store.get_job_result_messages(self.job_id)
            self._prefix = list(raw) if isinstance(raw, list) else []
        except Exception:
            logging.getLogger(__name__).exception(
                "读取已有步骤日志失败 id=%s", self.job_id
            )
            self._prefix = []
        if self._banner:
            self.capture.append_info(self._banner)
        self.capture.install()
        self._stop.clear()

        def _flush_loop() -> None:
            while not self._stop.wait(self._flush_interval):
                if self.capture.dirty_count() > 0:
                    self.flush(force=True)

        self._thread = threading.Thread(
            target=_flush_loop,
            daemon=True,
            name=f"job-log-flush-{self.job_id}",
        )
        self._thread.start()
        self.flush(force=True)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self.flush(force=True)
        self.capture.uninstall()
        return False

    def flush(self, *, force: bool = False) -> None:
        if not force and self.capture.dirty_count() < 50:
            return
        try:
            merged = list(self._prefix) + self.capture.snapshot()
            self.store.update_job_result_messages(self.job_id, merged)
            self.capture.mark_flushed()
        except Exception:
            logging.getLogger(__name__).exception(
                "flush job logs failed id=%s", self.job_id
            )


def split_job_log_phases(
    lines: list[dict[str, Any]] | None,
    *,
    form_status: str = "",
    activation_status: str = "",
) -> dict[str, list[dict[str, Any]]]:
    """Split combined result_messages into register / activation / nnc1 buckets."""
    buckets: dict[str, list[dict[str, Any]]] = {
        "register": [],
        "activation": [],
        "nnc1": [],
    }
    rows = list(lines or [])
    current = "register"
    form_st = str(form_status or "").strip().lower()
    act_st = str(activation_status or "").strip().lower()
    fail_bucket = ""
    if form_st == "failed":
        fail_bucket = "nnc1"
    elif act_st == "failed":
        fail_bucket = "activation"
    last_i = len(rows) - 1
    for i, raw in enumerate(rows):
        line = dict(raw) if isinstance(raw, dict) else {"level": "INFO", "message": str(raw)}
        msg = str(line.get("message") or "").strip()
        if ACTIVATION_LOG_BANNER in msg or msg == ACTIVATION_LOG_BANNER:
            current = "activation"
            continue
        if NNC1_LOG_BANNER in msg or msg == NNC1_LOG_BANNER:
            current = "nnc1"
            continue
        phase = str(line.get("phase") or "").strip()
        if phase in _PHASES:
            current = phase
            buckets[phase].append(line)
            continue
        level = str(line.get("level") or "").upper()
        if (
            i == last_i
            and fail_bucket
            and level in ("ERROR", "CRITICAL")
        ):
            buckets[fail_bucket].append(line)
            continue
        buckets[current].append(line)
    return buckets

