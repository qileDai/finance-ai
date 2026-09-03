"""ICRIS 账号激活 Worker — 每小时检查待激活任务的邮箱。"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


def contact_email_from_payload(payload: dict) -> str:
    """任务登记电邮：S03 填入 ICRIS 的 contact/applicant email。"""
    if not isinstance(payload, dict):
        return ""
    contact = payload.get("contact") or {}
    if not isinstance(contact, dict):
        contact = {}
    email = str(contact.get("email") or "").strip()
    if not email:
        applicant = payload.get("applicant") or {}
        if isinstance(applicant, dict):
            email = str(applicant.get("email") or "").strip()
    return email.lower()


def icris_credentials_from_payload(payload: dict) -> tuple[str, str]:
    """任务入库的 ICRIS 用户名/密码。"""
    if not isinstance(payload, dict):
        return "", ""
    account = payload.get("icris_account") or {}
    if not isinstance(account, dict):
        return "", ""
    username = str(account.get("username") or "").strip()
    password = str(account.get("password") or "").strip()
    return username, password


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
        # 激活阶段完成后，处理待填表任务
        self._check_form_pending_jobs()

    def _check_form_pending_jobs(self) -> None:
        """处理激活成功后待填表的任务。"""
        if self._stop.is_set():
            return
        jobs = self.store.get_jobs_pending_form()
        if not jobs:
            return
        logger.info("待填表任务 %d 个", len(jobs))
        for job in jobs:
            if self._stop.is_set():
                break
            try:
                self._process_form_job(job)
            except Exception as e:
                logger.error("处理填表任务 #%s 异常: %s", job.get("id"), e)

    def _process_form_job(self, job: dict) -> None:
        job_id = int(job.get("id") or 0)
        if not job_id:
            return

        # 从 payload_json 取账号密码和公司材料
        payload_str = str(job.get("payload_json") or "")
        if not payload_str:
            self.store.mark_job_form_failed(job_id, "无 payload 数据")
            self._notify_form_result(job, ok=False, detail="无 payload 数据")
            return

        try:
            data = json.loads(payload_str)
        except (TypeError, ValueError, json.JSONDecodeError):
            self.store.mark_job_form_failed(job_id, "payload_json 解析失败")
            self._notify_form_result(job, ok=False, detail="payload_json 解析失败")
            return

        # 合并后台配置的默认办事处地址（用户没填的字段用后台默认值）
        from src.materials.aggregator import apply_default_office
        apply_default_office(data)

        account_info = (data.get("icris_account") or {}) if isinstance(data, dict) else {}
        username = str(account_info.get("username") or "").strip()
        password = str(account_info.get("password") or "").strip()
        if not username or not password:
            self.store.mark_job_form_failed(job_id, "无账号密码，无法登录填表")
            self._notify_form_result(job, ok=False, detail="无账号密码")
            return

        # 准备截图目录
        from config.settings import PROJECT_ROOT
        shot_dir = PROJECT_ROOT / "data" / "icris_form_screenshots"
        shot_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        shot_file = shot_dir / f"form_{job_id}_{stamp}.png"

        # 登录填表
        from src.email.imap_client import IcrisAccount
        from src.browser.icris_nnc1_form import IcrisNnc1FormBot

        account = IcrisAccount(username=username, password=password)
        bot = IcrisNnc1FormBot()
        try:
            ok, detail = asyncio.run(
                bot.run(
                    account,
                    data,
                    force_isolated=False,
                    screenshot_path=str(shot_file),
                )
            )
        except RuntimeError:
            # 无事件循环环境，用新线程跑
            ok, detail = self._run_in_thread(
                bot.run, account, data,
                force_isolated=False,
                screenshot_path=str(shot_file),
            )
        except Exception as e:
            ok, detail = False, str(e)

        if ok:
            self.store.mark_job_form_filled(job_id, str(shot_file))
            logger.info("任务 #%s 填表成功，截图: %s", job_id, shot_file)
            self._notify_form_result(job, ok=True, detail=str(shot_file))
        else:
            self.store.mark_job_form_failed(job_id, f"填表失败: {detail}")
            logger.error("任务 #%s 填表失败: %s", job_id, detail)
            self._notify_form_result(job, ok=False, detail=detail)

    def _notify_form_result(self, job: dict, *, ok: bool, detail: str) -> None:
        """填表结果通知到企微内部群（无配置则跳过）。优先群机器人 Webhook。"""
        from config.settings import settings
        webhook_url = (settings.icris_review_webhook_url or "").strip()
        chat_id = (settings.icris_review_notify_chat_id or "").strip()
        if not webhook_url and not chat_id:
            logger.info("填表通知未配置 webhook/chat_id，跳过")
            return
        try:
            from src.wework.client import WeWorkClient
            client = WeWorkClient()
            job_id = int(job.get("id") or 0)
            company = str(job.get("company_name") or "")
            status_text = "填表成功" if ok else "填表失败"
            msg = (
                f"【ICRIS 填表通知】\n"
                f"任务 #{job_id}\n"
                f"公司: {company}\n"
                f"状态: {status_text}\n"
                f"详情: {detail[:200]}"
            )
            sent = False
            if webhook_url:
                try:
                    client.send_webhook_text(webhook_url, msg)
                    sent = True
                except Exception as e:
                    logger.warning("填表通知 Webhook 发送失败，回退应用消息: %s", e)
            if not sent and chat_id:
                client.send_group_text(chat_id, msg)
        except Exception as e:
            logger.warning("填表通知发送失败: %s", e)

    def _process_one_job(self, job: dict) -> None:
        job_id = int(job.get("id") or 0)
        if not job_id:
            return

        # 从 payload_json 取登记邮箱与 ICRIS 账号
        payload_str = str(job.get("payload_json") or "")
        payload: dict = {}
        if payload_str:
            try:
                loaded = json.loads(payload_str)
                if isinstance(loaded, dict):
                    payload = loaded
            except Exception:
                payload = {}

        contact_email = contact_email_from_payload(payload)
        icris_user, icris_pass = icris_credentials_from_payload(payload)

        if not contact_email:
            logger.warning("任务 #%s 无注册邮箱，跳过激活", job_id)
            self.store.mark_job_activation_failed(job_id, "无注册邮箱")
            return

        if not icris_user:
            logger.warning("任务 #%s 无 ICRIS 用户名，本轮跳过", job_id)
            return

        # 按任务电邮查 IMAP 配置（只读该任务自己的邮箱）
        account = self.store.get_email_account_by_address(contact_email)
        if not account:
            logger.warning("任务 #%s 邮箱 %s 未配置 IMAP", job_id, contact_email)
            self.store.mark_job_activation_failed(
                job_id, f"邮箱未配置 IMAP: {contact_email}"
            )
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

        # 检查激活邮件：必须用戶名稱与入库账号一致
        from src.email.imap_client import EmailClient

        client = EmailClient()
        link = client.fetch_activation_link(
            account, since_date, expected_username=icris_user
        )
        self.store.mark_job_activation_checked(job_id)

        if not link:
            logger.info("任务 #%s 暂无匹配 %s 的激活邮件，等下次检查", job_id, icris_user)
            return

        # 浏览器打开链接并用该任务密码登录激活
        logger.info("任务 #%s 开始浏览器激活（账号 %s）", job_id, icris_user)
        from src.browser.icris_activation import activate_icris_account

        try:
            ok, detail = asyncio.run(
                activate_icris_account(link, username=icris_user, password=icris_pass)
            )
        except RuntimeError as e:
            # 无事件循环环境，用新线程跑
            logger.warning("无事件循环，新线程执行激活: %s", e)
            ok, detail = self._run_in_thread(
                activate_icris_account,
                link,
                username=icris_user,
                password=icris_pass,
            )

        if ok:
            self.store.mark_job_activated(job_id)
            logger.info("任务 #%s 激活成功", job_id)
        else:
            self.store.mark_job_activation_failed(job_id, f"激活失败: {detail}")
            logger.error("任务 #%s 激活失败: %s", job_id, detail)

    def _run_in_thread(self, coro, *args, **kwargs):
        """在新线程的事件循环里运行协程。"""
        result: tuple[bool, str] = (False, "unknown")

        def _run() -> None:
            nonlocal result
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(coro(*args, **kwargs))
            finally:
                loop.close()

        t = threading.Thread(target=_run)
        t.start()
        t.join(timeout=120)
        return result
