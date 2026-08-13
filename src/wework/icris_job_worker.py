"""ICRIS 注册任务串行 Worker（SQLite 队列）"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from config.settings import settings
from src.storage.db import ExternalGroupStore
from src.wework.external_workflow import ExternalGroupWorkflow

logger = logging.getLogger(__name__)


@dataclass
class IcrisJobWorker:
    store: ExternalGroupStore = field(default_factory=ExternalGroupStore)
    workflow: ExternalGroupWorkflow = field(default_factory=ExternalGroupWorkflow)
    _thread: threading.Thread | None = None
    _stop: threading.Event = field(default_factory=threading.Event)
    alive: bool = False
    last_job_id: int | None = None
    last_error: str = ""

    def start(self, *, blocking: bool = False) -> None:
        if not settings.icris_worker_enabled:
            logger.info("ICRIS Worker 未启用（ICRIS_WORKER_ENABLED=false）")
            return
        if self._thread and self._thread.is_alive():
            return

        # 进程重启：回收遗留 running
        recovered = self.store.reset_stale_running_jobs(older_than_minutes=0)
        if recovered:
            logger.warning("ICRIS Worker 回收 stale running 任务: %d", recovered)

        self._stop.clear()

        def _loop() -> None:
            self.alive = True
            poll = max(0.5, float(settings.icris_worker_poll_seconds or 3.0))
            logger.info(
                "ICRIS Worker 已启动 poll=%.1fs max_attempts=%s",
                poll,
                settings.icris_job_max_attempts,
            )
            while not self._stop.is_set():
                try:
                    job = self.store.claim_next_job()
                    if job:
                        self._process_job(job)
                    else:
                        self._stop.wait(poll)
                except Exception:
                    logger.exception("ICRIS Worker 循环异常")
                    self._stop.wait(poll)
            self.alive = False
            logger.info("ICRIS Worker 已停止")

        if blocking:
            _loop()
            return

        self._thread = threading.Thread(
            target=_loop, daemon=True, name="icris-job-worker"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self.alive = False

    def _backoff_iso(self, attempts: int) -> str:
        base = float(settings.icris_job_retry_backoff_seconds or 30.0)
        delay = base * (2 ** max(0, attempts - 1))
        delay = min(delay, 3600.0)
        return (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()

    def _process_job(self, job: dict[str, Any]) -> None:
        job_id = int(job["id"])
        roomid = str(job.get("roomid") or "")
        attempts = int(job.get("attempts") or 0)
        max_attempts = int(job.get("max_attempts") or settings.icris_job_max_attempts or 3)
        self.last_job_id = job_id
        t0 = time.monotonic()
        dry_run = bool(int(job.get("dry_run", 1) or 0))
        allow_submit = bool(int(job.get("allow_submit", 0) or 0)) and (not dry_run)
        logger.info(
            "ICRIS job start id=%s roomid=%s attempt=%s/%s dry_run=%s allow_submit=%s",
            job_id,
            roomid,
            attempts,
            max_attempts,
            dry_run,
            allow_submit,
        )
        package_dir = str(job.get("package_dir") or "")
        try:
            ctx = self.workflow.run_icris_job(job, force_isolated_browser=True)
            package_dir = str(ctx.package_dir or package_dir)
            msgs = list(getattr(ctx, "messages", None) or [])
            self.store.mark_job_succeeded(
                job_id, package_dir=package_dir, result_messages=msgs
            )
            self.store.set_group_status(roomid, "HANDOFF")
            self.workflow.notify_job_result(
                job, ok=True, package_dir=package_dir
            )
            elapsed = time.monotonic() - t0
            logger.info(
                "ICRIS job ok id=%s roomid=%s duration=%.1fs package=%s",
                job_id,
                roomid,
                elapsed,
                package_dir,
            )
            self.last_error = ""
        except Exception as e:
            err = str(e)
            self.last_error = err[:500]
            elapsed = time.monotonic() - t0
            logger.exception(
                "ICRIS job fail id=%s roomid=%s attempt=%s duration=%.1fs: %s",
                job_id,
                roomid,
                attempts,
                elapsed,
                err,
            )
            requeue = attempts < max_attempts
            available_at = self._backoff_iso(attempts) if requeue else ""
            screenshot_path = ""
            from src.browser.icris_errors import IcrisFlowError

            if isinstance(e, IcrisFlowError):
                screenshot_path = e.screenshot_path or ""
            msgs: list[str] = []
            # 失败时尽量保留已有步骤日志（若异常对象挂了 ctx）
            ctx_fail = getattr(e, "ctx", None)
            if ctx_fail is not None:
                msgs = list(getattr(ctx_fail, "messages", None) or [])
            self.store.mark_job_failed(
                job_id,
                error=err,
                requeue=requeue,
                available_at=available_at,
                package_dir=package_dir,
                screenshot_path=screenshot_path,
                result_messages=msgs or None,
            )
            if screenshot_path:
                logger.warning(
                    "ICRIS job fail screenshot id=%s path=%s", job_id, screenshot_path
                )
            if requeue:
                logger.warning(
                    "ICRIS job requeue id=%s next_at=%s", job_id, available_at
                )
            else:
                self.store.set_group_status(roomid, "FAILED")
                notify_err = err
                if screenshot_path:
                    notify_err = f"{err}\n截图: {screenshot_path}"
                self.workflow.notify_job_result(
                    job, ok=False, package_dir=package_dir, error=notify_err
                )

    def status_payload(self) -> dict[str, Any]:
        stats = self.store.registration_job_stats()
        return {
            "enabled": bool(settings.icris_worker_enabled),
            "alive": self.alive,
            "pending_count": stats.get("pending_count", 0),
            "running_count": stats.get("running_count", 0),
            "running_job_id": stats.get("running_job_id"),
            "running_roomid": stats.get("running_roomid", ""),
            "last_job_id": self.last_job_id,
            "last_error": self.last_error,
            "counts": stats.get("counts", {}),
        }
