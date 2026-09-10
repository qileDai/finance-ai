"""ICRIS 账号激活 Worker — 每小时扫信入库；队列空时按先存链接先激活再填表。"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_ACTIVATION_MAX_ATTEMPTS = 3


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


def parse_job_payload(job: dict) -> dict:
    payload_str = str(job.get("payload_json") or "")
    if not payload_str:
        return {}
    try:
        loaded = json.loads(payload_str)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _activation_fail_kind(detail: str) -> str:
    text = detail or ""
    lower = text.lower()
    if "不是启动帐户链接" in text:
        return "bad_link"
    if "启动帐户页需要用户名和密码" in text:
        return "missing_creds"
    if (
        "incorrect user" in lower
        or "用户名称或密码不正确" in text
        or "用戶名稱或密碼不正確" in text
        or "用户名或密码不正确" in text
        or "帳號或密碼不正確" in text
        or "帐号或密码不正确" in text
    ):
        return "credential"
    if "打开后不是启动帐户页" in text:
        return "stale_page"
    return "transient"


class IcrisActivationWorker:
    """扫激活邮件（小时）与队列空时 drain（激活+填表）。"""

    def __init__(
        self,
        store,
        interval_seconds: int | float | None = None,
        form_poll_seconds: float | None = None,
    ) -> None:
        from config.settings import settings

        self.store = store
        if interval_seconds is None:
            self.interval = float(
                getattr(settings, "icris_activation_poll_seconds", 3600) or 3600
            )
        else:
            self.interval = float(interval_seconds)
        self.form_poll = float(
            form_poll_seconds
            if form_poll_seconds is not None
            else getattr(settings, "icris_form_poll_seconds", 60.0) or 60.0
        )
        self._max_attempts = int(
            getattr(settings, "icris_activation_max_attempts", _ACTIVATION_MAX_ATTEMPTS)
            or _ACTIVATION_MAX_ATTEMPTS
        )
        self._thread: threading.Thread | None = None
        self._form_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._drain_lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        repaired = self.store.reset_stale_activating_jobs()
        if repaired:
            logger.warning("激活 worker 回收卡住的 activating: %d", repaired)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._activation_loop, daemon=True, name="icris-activation"
        )
        self._form_thread = threading.Thread(
            target=self._form_loop, daemon=True, name="icris-nnc1-form"
        )
        self._thread.start()
        self._form_thread.start()
        logger.info(
            "IcrisActivationWorker 启动，激活间隔 %ss，填表轮询 %ss",
            self.interval,
            self.form_poll,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)
        if self._form_thread:
            self._form_thread.join(timeout=5.0)

    def _activation_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._check_pending_jobs()
                self.drain()
            except Exception as e:
                logger.exception("激活 worker 异常: %s", e)
            self._stop.wait(self.interval)

    def _form_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.drain()
            except Exception as e:
                logger.exception("填表 worker 异常: %s", e)
            self._stop.wait(self.form_poll)

    def _check_pending_jobs(self) -> None:
        jobs = self.store.get_jobs_pending_activation()
        if not jobs:
            return
        logger.info("待扫信任务 %d 个", len(jobs))

        for job in jobs:
            if self._stop.is_set():
                break
            try:
                self._process_one_job(job)
            except Exception as e:
                logger.error("处理激活任务 #%s 异常: %s", job.get("id"), e)

    def drain(self) -> None:
        """注册队列空时：先填已激活未填的表，再按链接写入顺序激活+填表。"""
        if not self._drain_lock.acquire(blocking=False):
            return
        try:
            seen: set[int] = set()
            while not self._stop.is_set():
                if self.store.has_active_registration_queue():
                    return
                form_jobs = self.store.get_jobs_pending_form()
                if form_jobs:
                    jid = int(form_jobs[0].get("id") or 0)
                    if not jid or jid in seen:
                        return
                    seen.add(jid)
                    self._process_form_job(form_jobs[0])
                    continue
                ready = self.store.get_jobs_ready_to_activate()
                if not ready:
                    return
                jid = int(ready[0].get("id") or 0)
                if not jid or jid in seen:
                    return
                seen.add(jid)
                claimed = self.store.claim_job_activation(jid)
                if not claimed:
                    return
                if not self._activate_claimed_job(claimed):
                    continue
                if self.store.has_active_registration_queue():
                    return
                row = self.store.get_registration_job(jid) or {}
                if (
                    str(row.get("activation_status") or "") == "activated"
                    and str(row.get("form_status") or "") == "pending"
                ):
                    self._process_form_job(row)
        finally:
            self._drain_lock.release()

    def _check_form_pending_jobs(self) -> None:
        """处理激活成功后待填表的任务。有可跑的注册则整轮跳过。"""
        if self._stop.is_set():
            return
        jobs = self.store.get_jobs_pending_form()
        if not jobs:
            return
        if self.store.has_active_registration_queue():
            logger.info(
                "注册队列未空，待填表 %d 个暂缓",
                len(jobs),
            )
            return
        logger.info("待填表任务 %d 个", len(jobs))
        for job in jobs:
            if self._stop.is_set():
                break
            if self.store.has_active_registration_queue():
                logger.info("注册队列有新任务，停止本轮后续 NNC1")
                break
            try:
                self._process_form_job(job)
            except Exception as e:
                logger.error("处理填表任务 #%s 异常: %s", job.get("id"), e)

    def _process_form_job(self, job: dict) -> None:
        job_id = int(job.get("id") or 0)
        if not job_id:
            return
        row = self.store.get_registration_job(job_id) or job
        if str(row.get("form_status") or "") == "filled":
            logger.info("任务 #%s 已填表，跳过", job_id)
            return
        if str(row.get("form_status") or "") != "pending":
            return
        if str(row.get("activation_status") or "") != "activated":
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
        bot.job_id = job_id
        bot._job_screenshot_path = str(shot_file)
        from config.settings import settings
        from src.browser.cdp_session import SessionWatchdog, hold_cdp_lock

        with hold_cdp_lock("nnc1"):
            if self.store.has_active_registration_queue():
                logger.info(
                    "任务 #%s 拿锁后发现注册队列未空，放锁跳过 NNC1",
                    job_id,
                )
                return
            fill = float(
                getattr(settings, "icris_cdp_session_timeout_seconds", 1500) or 1500
            )
            keep = float(getattr(settings, "browser_keep_open_seconds", 15) or 15)
            wd = SessionWatchdog(fill + keep + 30.0)
            wd.start()
            from src.storage.db import format_job_run_duration

            t0 = time.monotonic()
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
                ok, detail = self._run_in_thread(
                    bot.run,
                    account,
                    data,
                    force_isolated=False,
                    screenshot_path=str(shot_file),
                    join_timeout=fill + keep + 120.0,
                )
            except Exception as e:
                ok, detail = False, str(e)
            finally:
                wd.stop()
            nnc1_duration = format_job_run_duration(time.monotonic() - t0)

        prelim_notified = bool(getattr(bot, "_prelim_notified", False))
        from src.browser.icris_errors import is_nnc1_loading_timeout

        if not ok and is_nnc1_loading_timeout(detail):
            boosted = self.store.requeue_job_form_loading_timeout(job_id)
            row = self.store.get_registration_job(job_id) or {}
            if str(row.get("form_status") or "") == "pending":
                logger.info(
                    "任务 #%s 载入中超时，已插队从登录重跑 boost=%s retries=%s",
                    job_id,
                    (boosted or row).get("form_boost"),
                    (boosted or row).get("nnc1_loading_retries"),
                )
                return

        if self.store.job_form_outcome_written(job_id):
            if ok:
                logger.info("任务 #%s 填表成功，截图: %s", job_id, shot_file)
                if not prelim_notified:
                    self._notify_form_result(job, ok=True, detail=str(shot_file))
            else:
                logger.error("任务 #%s 填表失败: %s", job_id, detail)
                if not prelim_notified:
                    self._notify_form_result(job, ok=False, detail=detail)
            return

        if ok:
            self.store.mark_job_form_filled(
                job_id, str(shot_file), nnc1_duration=nnc1_duration
            )
            logger.info("任务 #%s 填表成功，截图: %s", job_id, shot_file)
            if not prelim_notified:
                self._notify_form_result(job, ok=True, detail=str(shot_file))
        else:
            fail_shot = str(shot_file) if shot_file.is_file() else ""
            self.store.mark_job_form_failed(
                job_id,
                f"填表失败: {detail}",
                fail_shot,
                nnc1_duration=nnc1_duration,
            )
            logger.error("任务 #%s 填表失败: %s", job_id, detail)
            if fail_shot:
                logger.info("任务 #%s 填表失败截图: %s", job_id, fail_shot)
            if not prelim_notified:
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
        """扫信：只把匹配该单用户名的链接写入该行，不开浏览器。"""
        job_id = int(job.get("id") or 0)
        if not job_id:
            return
        live = self.store.get_registration_job(job_id) or job
        if str(live.get("status") or "") != "succeeded":
            return
        act = str(live.get("activation_status") or "")
        if act in ("activated", "failed", "activating"):
            return
        if act != "pending":
            return

        payload = parse_job_payload(live)
        contact_email = contact_email_from_payload(payload)
        icris_user, _icris_pass = icris_credentials_from_payload(payload)

        if not contact_email:
            logger.warning("任务 #%s 无注册邮箱，跳过激活", job_id)
            self.store.mark_job_activation_failed(job_id, "无注册邮箱")
            return

        if not icris_user:
            logger.warning("任务 #%s 无 ICRIS 用户名，本轮跳过", job_id)
            return

        stored_url = str(live.get("activation_url") or "").strip()
        stored_user = str(live.get("activation_username") or "").strip()
        if stored_url:
            if stored_user.lower() == icris_user.lower():
                logger.info("任务 #%s 已有匹配链接，跳过扫信", job_id)
                return
            logger.warning(
                "任务 #%s 已存链接用户名 %s 与账号 %s 不符，清空重搜",
                job_id,
                stored_user,
                icris_user,
            )
            self.store.clear_job_activation_url(job_id)

        account = self.store.get_email_account_by_address(contact_email)
        if not account:
            logger.warning("任务 #%s 邮箱 %s 未配置 IMAP", job_id, contact_email)
            self.store.mark_job_activation_failed(
                job_id, f"邮箱未配置 IMAP: {contact_email}"
            )
            return

        since_raw = str(
            live.get("activation_pending_at") or live.get("created_at") or ""
        )
        since_date: datetime | None = None
        try:
            if since_raw:
                since_date = datetime.fromisoformat(since_raw.replace("Z", "+00:00"))
        except Exception:
            pass

        if since_date:
            now_utc = datetime.now(timezone.utc)
            if now_utc - since_date > timedelta(days=7):
                self.store.mark_job_activation_failed(job_id, "激活超时（7天）")
                logger.warning("任务 #%s 激活超时", job_id)
                return

        from src.email.imap_client import EmailClient
        from src.browser.icris_activation import require_s06_activation_url

        client = EmailClient()
        link = client.fetch_activation_link(
            account, since_date, expected_username=icris_user
        )
        self.store.mark_job_activation_checked(job_id)

        if not link:
            logger.info("任务 #%s 暂无匹配 %s 的激活邮件，等下次检查", job_id, icris_user)
            return

        open_url, url_err = require_s06_activation_url(link)
        if url_err:
            self.store.mark_job_activation_failed(job_id, url_err)
            logger.error("任务 #%s %s", job_id, url_err)
            return

        saved = self.store.save_job_activation_url(job_id, open_url, icris_user)
        if saved:
            logger.info(
                "任务 #%s 已写入激活链接 用户名=%s",
                job_id,
                icris_user,
            )
        else:
            logger.info("任务 #%s 链接未写入（已激活或已有链接）", job_id)

    def _activate_claimed_job(self, job: dict) -> bool:
        """用该行已存链接+账密开浏览器。成功返回 True。"""
        job_id = int(job.get("id") or 0)
        payload = parse_job_payload(job)
        icris_user, icris_pass = icris_credentials_from_payload(payload)
        stored_url = str(job.get("activation_url") or "").strip()
        stored_user = str(job.get("activation_username") or "").strip()

        if not icris_user or not stored_url:
            self.store.release_job_activation_claim(job_id)
            self.store.clear_job_activation_url(job_id)
            logger.warning("任务 #%s 认领后缺少用户名或链接，已放回", job_id)
            return False

        if stored_user.lower() != icris_user.lower():
            self.store.release_job_activation_claim(job_id)
            self.store.clear_job_activation_url(job_id)
            logger.warning(
                "任务 #%s 链接用户名 %s 与账号 %s 不符，清空",
                job_id,
                stored_user,
                icris_user,
            )
            return False

        if self.store.has_active_registration_queue():
            self.store.release_job_activation_claim(job_id)
            logger.info("注册队列未空，任务 #%s 放回待激活", job_id)
            return False

        from src.browser.icris_activation import require_s06_activation_url

        open_url, url_err = require_s06_activation_url(stored_url)
        if url_err:
            self.store.mark_job_activation_failed(job_id, url_err)
            logger.error("任务 #%s %s", job_id, url_err)
            return False

        logger.info(
            "任务 #%s 开始浏览器激活（账号 %s）",
            job_id,
            icris_user,
        )
        ok, detail = self._run_activate_browser(open_url, icris_user, icris_pass)

        if ok:
            self.store.mark_job_activated(job_id)
            logger.info("任务 #%s 激活成功", job_id)
            return True

        kind = _activation_fail_kind(str(detail or ""))
        attempts = int(job.get("activation_attempts") or 0)
        if kind in ("credential", "bad_link", "missing_creds"):
            self.store.mark_job_activation_failed(job_id, f"激活失败: {detail}")
            logger.error("任务 #%s 激活失败: %s", job_id, detail)
            return False

        if kind == "stale_page" or attempts >= self._max_attempts:
            if attempts >= self._max_attempts and kind != "stale_page":
                self.store.mark_job_activation_failed(
                    job_id, f"激活失败（已重试 {attempts} 次）: {detail}"
                )
                logger.error("任务 #%s 激活失败耗尽重试: %s", job_id, detail)
                return False
            self.store.release_job_activation_claim(job_id)
            self.store.clear_job_activation_url(job_id)
            logger.warning("任务 #%s 链接失效，已清空等下次扫信: %s", job_id, detail)
            return False

        self.store.release_job_activation_claim(job_id)
        logger.warning("任务 #%s 激活瞬时失败，保持链接待重试: %s", job_id, detail)
        return False

    def _run_activate_browser(
        self, url: str, username: str, password: str
    ) -> tuple[bool, str]:
        from src.browser.icris_activation import activate_icris_account

        try:
            return asyncio.run(
                activate_icris_account(url, username=username, password=password)
            )
        except RuntimeError as e:
            logger.warning("无事件循环，新线程执行激活: %s", e)
            return self._run_in_thread(
                activate_icris_account,
                url,
                username=username,
                password=password,
                join_timeout=180,
            )

    def _run_in_thread(self, coro, *args, join_timeout: float = 120, **kwargs):
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
        t.join(timeout=float(join_timeout))
        return result
