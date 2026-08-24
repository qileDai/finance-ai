"""ICRIS 账号激活 Worker — 每小时检查待激活任务的邮箱。"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


class IcrisActivationWorker:
    """每小时检查待激活的注册任务邮箱，提取激活链接并用浏览器点击。"""

    def __init__(self, store, interval_seconds: int = 3600) -> None:
        self.store = store
        self.interval = interval_seconds
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("IcrisActivationWorker 启动，间隔 %ss", self.interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)

    def _loop(self) -> None:
        # 启动后先等 60 秒再开始（避免和 worker 启动冲突）
        self._stop.wait(60)
        while not self._stop.is_set():
            try:
                self._check_pending_jobs()
            except Exception as e:
                logger.exception("激活 worker 异常: %s", e)
            self._stop.wait(self.interval)

    def _check_pending_jobs(self) -> None:
        jobs = self.store.get_jobs_pending_activation()
        if not jobs:
            return
        logger.info("待激活任务 %d 个", len(jobs))

        for job in jobs:
            if self._stop.is_set():
                break
            try:
                self._process_one_job(job)
            except Exception as e:
                logger.error("处理激活任务 #%s 异常: %s", job.get("id"), e)

    def _process_one_job(self, job: dict) -> None:
        job_id = int(job.get("id") or 0)
        if not job_id:
            return

        # 从 payload_json 取注册邮箱
        payload_str = str(job.get("payload_json") or "")
        contact_email = ""
        if payload_str:
            try:
                payload = json.loads(payload_str)
                contact = payload.get("contact") or {}
                contact_email = str(contact.get("email") or "").strip()
            except Exception:
                pass

        if not contact_email:
            logger.warning("任务 #%s 无注册邮箱，跳过激活", job_id)
            self.store.mark_job_activation_failed(job_id, "无注册邮箱")
            return

        # 查邮箱配置
        account = self.store.get_email_account_by_address(contact_email)
        if not account:
            logger.info("任务 #%s 邮箱 %s 未配置 IMAP，跳过", job_id, contact_email)
            return

        # 注册时间
        created_at_str = str(job.get("created_at") or "")
        since_date: datetime | None = None
        try:
            if created_at_str:
                since_date = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        except Exception:
            pass

        # 7 天超时
        if since_date:
            now_utc = datetime.now(timezone.utc)
            if now_utc - since_date > timedelta(days=7):
                self.store.mark_job_activation_failed(job_id, "激活超时（7天）")
                logger.warning("任务 #%s 激活超时", job_id)
                return

        # 检查激活邮件
        from src.email.imap_client import EmailClient

        client = EmailClient()
        link = client.fetch_activation_link(account, since_date)
        self.store.mark_job_activation_checked(job_id)

        if not link:
            logger.info("任务 #%s 暂无激活邮件，等下次检查", job_id)
            return

        # 浏览器点击激活
        logger.info("任务 #%s 开始浏览器激活", job_id)
        from src.browser.icris_activation import activate_icris_account

        try:
            ok, detail = asyncio.run(activate_icris_account(link))
        except RuntimeError as e:
            # 无事件循环环境，用新线程跑
            logger.warning("无事件循环，新线程执行激活: %s", e)
            ok, detail = self._run_in_thread(activate_icris_account, link)

        if ok:
            self.store.mark_job_activated(job_id)
            logger.info("任务 #%s 激活成功", job_id)
        else:
            self.store.mark_job_activation_failed(job_id, f"激活失败: {detail}")
            logger.error("任务 #%s 激活失败: %s", job_id, detail)

    def _run_in_thread(self, coro, *args):
        """在新线程的事件循环里运行协程。"""
        result: tuple[bool, str] = (False, "unknown")

        def _run() -> None:
            nonlocal result
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(coro(*args))
            finally:
                loop.close()

        t = threading.Thread(target=_run)
        t.start()
        t.join(timeout=120)
        return result
