"""定期清空已结束任务的步骤日志（result_messages），不删任务行。"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)


class JobLogCleanupWorker:
    """bot 进程守护线程：按 TTL 清空过期步骤日志，随后 WAL checkpoint。"""

    def __init__(
        self,
        store: Any,
        *,
        retention_days: int | None = None,
        interval_seconds: float | None = None,
    ) -> None:
        from config.settings import settings

        self.store = store
        if retention_days is None:
            self.retention_days = int(getattr(settings, "job_log_retention_days", 14) or 0)
        else:
            self.retention_days = int(retention_days)
        if interval_seconds is None:
            self.interval = float(
                getattr(settings, "job_log_cleanup_interval_seconds", 86400.0) or 86400.0
            )
        else:
            self.interval = float(interval_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.retention_days <= 0:
            logger.info("步骤日志清理未启用（JOB_LOG_RETENTION_DAYS=0）")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="job-log-cleanup"
        )
        self._thread.start()
        logger.info(
            "步骤日志清理已启动 retention=%sd interval=%.0fs",
            self.retention_days,
            self.interval,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)

    def run_once(self) -> int:
        n = int(self.store.purge_old_job_result_messages(self.retention_days) or 0)
        try:
            self.store.wal_checkpoint_truncate()
        except Exception:
            logger.exception("wal_checkpoint 失败")
        if n:
            logger.info("步骤日志清理 %d 条任务", n)
        return n

    def _loop(self) -> None:
        try:
            self.run_once()
        except Exception:
            logger.exception("步骤日志清理异常")
        interval = max(1.0, self.interval)
        while not self._stop.wait(interval):
            try:
                self.run_once()
            except Exception:
                logger.exception("步骤日志清理异常")
