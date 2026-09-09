"""ICRIS 注册任务串行 Worker（SQLite 队列）"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from config.settings import settings
from src.storage.db import ExternalGroupStore, format_job_run_duration
from src.wework.external_workflow import ExternalGroupWorkflow
from src.wework.job_log_capture import JobLogCapture

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
    _activation_worker: Any = None

    def start(self, *, blocking: bool = False) -> None:
        if not settings.icris_worker_enabled:
            logger.info("ICRIS Worker 未启用（ICRIS_WORKER_ENABLED=false）")
            return
        if self._thread and self._thread.is_alive():
            return

        # 进程重启：已拒绝僵尸单修回 failed，再回收真正卡住的 running
        repaired = self.store.repair_rejected_jobs()
        if repaired:
            logger.warning("ICRIS Worker 修复已拒绝僵尸单: %d", repaired)
        recovered = self.store.reset_stale_running_jobs(older_than_minutes=0)
        if recovered:
            logger.warning("ICRIS Worker 回收 stale running 任务: %d", recovered)
        from src.browser.cdp_lock import cdp_lease_alive

        if not cdp_lease_alive():
            orphans = self.store.fail_orphan_awaiting_review()
            if orphans:
                logger.warning(
                    "ICRIS Worker 清理僵尸 awaiting_review: %d", orphans
                )

        # 启动激活检查 worker（每小时检查待激活任务的邮箱）
        from src.wework.icris_activation_worker import IcrisActivationWorker
        self._activation_worker = IcrisActivationWorker(self.store)
        self._activation_worker.start()

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
                    if not self.store.peek_claimable_registration():
                        self._stop.wait(poll)
                        continue
                    self._claim_and_run()
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
        if self._activation_worker:
            self._activation_worker.stop()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self.alive = False

    def _claim_and_run(self) -> None:
        """先拿 CDP 锁再 claim，拿不到锁时任务保持 pending。"""
        from config.settings import settings
        from src.browser.cdp_session import SessionWatchdog, hold_cdp_lock

        with hold_cdp_lock("registration"):
            job = self.store.claim_next_job()
            if not job:
                return
            fill = float(
                getattr(settings, "icris_cdp_session_timeout_seconds", 1500) or 1500
            )
            keep = float(getattr(settings, "browser_keep_open_seconds", 15) or 15)
            wd = SessionWatchdog(
                fill + keep + 30.0,
                job_id=int(job["id"]),
                store=self.store,
            )
            wd.start()
            try:
                self._process_job(job)
            finally:
                wd.stop()

    def _backoff_iso(self, attempts: int) -> str:
        base = float(settings.icris_job_retry_backoff_seconds or 30.0)
        delay = base * (2 ** max(0, attempts - 1))
        delay = min(delay, 3600.0)
        return (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()

    def _flush_job_logs(self, job_id: int, capture: JobLogCapture, *, force: bool = False) -> None:
        if not force and capture.dirty_count() < 50:
            return
        try:
            self.store.update_job_result_messages(job_id, capture.snapshot())
            capture.mark_flushed()
        except Exception:
            logger.exception("flush job logs failed id=%s", job_id)

    def _process_job(self, job: dict[str, Any]) -> None:
        job_id = int(job["id"])
        roomid = str(job.get("roomid") or "")
        attempts = int(job.get("attempts") or 0)
        max_attempts = int(job.get("max_attempts") or settings.icris_job_max_attempts or 3)
        self.last_job_id = job_id
        t0 = time.monotonic()
        dry_run = bool(int(job.get("dry_run", 1) or 0))
        allow_submit = bool(int(job.get("allow_submit", 0) or 0)) and (not dry_run)
        source = str(job.get("source") or "").strip() or "-"
        logger.info(
            "ICRIS job start id=%s roomid=%s source=%s attempt=%s/%s "
            "dry_run=%s allow_submit=%s isolated=false(cdp)",
            job_id,
            roomid,
            source,
            attempts,
            max_attempts,
            dry_run,
            allow_submit,
        )
        package_dir = str(job.get("package_dir") or "")
        capture = JobLogCapture()
        capture.install()
        stop_flush = threading.Event()

        def _flush_loop() -> None:
            while not stop_flush.wait(5.0):
                if capture.dirty_count() > 0:
                    self._flush_job_logs(job_id, capture, force=True)

        flush_thread = threading.Thread(
            target=_flush_loop, daemon=True, name=f"job-log-flush-{job_id}"
        )
        flush_thread.start()
        try:
            if self.store.get_job_status(job_id) == "cancelled":
                logger.warning("ICRIS job 开始前已被取消 id=%s", job_id)
                return
            # 与 python main.py --step register 一致：走 Chrome CDP + stealth，避免 s02 指纹卡加载
            ctx = self.workflow.run_icris_job(job, force_isolated_browser=False)
            package_dir = str(ctx.package_dir or package_dir)
            capture.merge_ctx_messages(list(getattr(ctx, "messages", None) or []))
            msgs = capture.snapshot()
            # 任务被取消则不覆盖为 succeeded
            cur_status = self.store.get_job_status(job_id)
            review_status = self.store.get_job_review_status(job_id)
            elapsed = time.monotonic() - t0
            if (
                cur_status in ("cancelled", "failed")
                or (review_status or "").lower() == "rejected"
            ):
                logger.warning(
                    "ICRIS job 终态不覆盖为 succeeded id=%s status=%s review=%s",
                    job_id,
                    cur_status,
                    review_status,
                )
            else:
                self.store.mark_job_succeeded(
                    job_id,
                    package_dir=package_dir,
                    result_messages=msgs,
                    esubmit_screenshot_path=getattr(ctx, "esubmit_screenshot_path", "") or "",
                    success_screenshot_path=getattr(ctx, "success_screenshot_path", "") or "",
                    run_duration=format_job_run_duration(elapsed),
                )
                # 标记待激活：激活 worker 会每小时检查邮箱
                self.store.mark_job_activation_pending(job_id)
                self.store.set_group_status(roomid, "HANDOFF")
                self.workflow.notify_job_result(
                    job, ok=True, package_dir=package_dir
                )
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
            screenshot_path = ""
            from src.browser.icris_errors import (
                IcrisFlowError,
                register_failure_should_requeue,
            )

            requeue = register_failure_should_requeue(
                e, attempts, max_attempts
            )
            available_at = self._backoff_iso(attempts) if requeue else ""

            if isinstance(e, IcrisFlowError):
                screenshot_path = e.screenshot_path or ""
                # 审核拒绝/超时 / 证件号已登记：不重跑，直接关闭任务
                if getattr(e, "no_requeue", False):
                    requeue = False
                    available_at = ""
                    logger.warning(
                        "ICRIS job 不重跑 id=%s err=%s",
                        job_id,
                        err[:80],
                    )
                if getattr(e, "id_already_registered", False):
                    self.store.mark_job_id_already_registered(job_id)
                    logger.warning(
                        "ICRIS job 证件号已登记 id=%s", job_id
                    )
            # 审核等待中 bot 异常退出（如浏览器崩溃）：不重跑，标记 failed
            if requeue:
                cur_status = self.store.get_job_status(job_id)
                if cur_status == "awaiting_review":
                    requeue = False
                    available_at = ""
                    logger.warning(
                        "ICRIS job 审核等待中异常退出 id=%s，标记 failed 不重跑",
                        job_id,
                    )
            # 取消 / 审核拒绝：保持终态，禁止自动重跑覆盖为 pending
            cur_status = self.store.get_job_status(job_id)
            review_status = self.store.get_job_review_status(job_id)
            if cur_status == "cancelled":
                logger.warning(
                    "ICRIS job 已被取消 id=%s，保持 cancelled 不标记 failed",
                    job_id,
                )
                stop_flush.set()
                flush_thread.join(timeout=2.0)
                capture.uninstall()
                return
            if (review_status or "").lower() == "rejected":
                requeue = False
                available_at = ""
                logger.warning(
                    "ICRIS job 已拒绝不重跑 id=%s status=%s",
                    job_id,
                    cur_status,
                )
                if cur_status == "failed":
                    stop_flush.set()
                    flush_thread.join(timeout=2.0)
                    capture.uninstall()
                    return
            ctx_fail = getattr(e, "ctx", None)
            if ctx_fail is not None:
                capture.merge_ctx_messages(
                    list(getattr(ctx_fail, "messages", None) or [])
                )
            capture.append_error(err)
            msgs = capture.snapshot()
            self.store.mark_job_failed(
                job_id,
                error=err,
                requeue=requeue,
                available_at=available_at,
                package_dir=package_dir,
                screenshot_path=screenshot_path,
                success_screenshot_path=screenshot_path,
                result_messages=msgs or None,
                run_duration=format_job_run_duration(elapsed),
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
        finally:
            stop_flush.set()
            flush_thread.join(timeout=2.0)
            capture.uninstall()

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
