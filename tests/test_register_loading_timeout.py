"""注册切步载入超时：失败写入 last_error，只自动重跑一次。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from src.browser.icris_errors import (
    IcrisFlowError,
    IcrisLoadingTimeoutError,
    IcrisStepLoadError,
    is_icris_format_invalid_error,
    is_icris_username_taken_error,
    is_id_already_registered_error,
    register_failure_should_requeue,
    register_loading_timeout_message,
)
from src.browser.icris_registration import IcrisRegistrationBot
from src.storage.db import ExternalGroupStore
from src.wework.icris_job_worker import IcrisJobWorker


class TestRegisterLoadingTimeoutHelpers(unittest.TestCase):
    def test_message_includes_step(self):
        self.assertEqual(
            register_loading_timeout_message("s04"),
            "s04 页面载入中超时，遮罩仍在",
        )

    def test_timeout_requeue_once(self):
        err = IcrisLoadingTimeoutError(register_loading_timeout_message("s04"))
        self.assertTrue(register_failure_should_requeue(err, 1, 3))
        self.assertFalse(register_failure_should_requeue(err, 2, 3))

    def test_other_errors_keep_max_attempts(self):
        err = IcrisStepLoadError("未进入身份证明页: https://example")
        self.assertTrue(register_failure_should_requeue(err, 1, 3))
        self.assertTrue(register_failure_should_requeue(err, 2, 3))
        self.assertFalse(register_failure_should_requeue(err, 3, 3))
        no_rq = IcrisFlowError("审核拒绝", no_requeue=True)
        self.assertFalse(register_failure_should_requeue(no_rq, 1, 3))
        fmt = IcrisFlowError("s04 香港身分證號碼格式不正確！", no_requeue=True)
        self.assertFalse(register_failure_should_requeue(fmt, 1, 3))
        taken = IcrisFlowError("s02 用户名称已存在")
        self.assertTrue(register_failure_should_requeue(taken, 1, 3))
        self.assertTrue(register_failure_should_requeue(taken, 2, 3))
        self.assertFalse(register_failure_should_requeue(taken, 3, 3))

    def test_format_invalid_detect(self):
        self.assertTrue(
            is_icris_format_invalid_error(["香港身分證號碼格式不正確！"])
        )
        self.assertTrue(is_icris_format_invalid_error("身份证号码格式不正确"))
        self.assertFalse(
            is_icris_format_invalid_error(
                ["相同的身分證件號碼已在系統中登記！"]
            )
        )
        self.assertFalse(is_icris_format_invalid_error(["請輸入身分證號碼"]))
        self.assertFalse(
            is_id_already_registered_error(["香港身分證號碼格式不正確！"])
        )

    def test_username_taken_detect(self):
        self.assertTrue(is_icris_username_taken_error(["用户名称已存在"]))
        self.assertTrue(is_icris_username_taken_error("用戶名稱已被使用"))
        self.assertFalse(is_icris_username_taken_error(["香港身分證號碼格式不正確！"]))

    def test_nav_step_label(self):
        bot = IcrisRegistrationBot(llm=MagicMock())
        self.assertEqual(bot._nav_step_label("s04"), "s04")
        self.assertEqual(
            bot._nav_step_label(
                None,
                "https://www.e-services.cr.gov.hk/ICRIS3EF/system/registration/s03.do",
            ),
            "s04",
        )
        self.assertEqual(
            bot._nav_step_label(
                None,
                "https://www.e-services.cr.gov.hk/ICRIS3EF/system/registration/s04.do",
            ),
            "s03a",
        )


class TestRegisterStepReadyAndMissing(unittest.IsolatedAsyncioTestCase):
    async def test_wait_step_ready_false_while_overlay(self):
        bot = IcrisRegistrationBot(llm=MagicMock())
        page = MagicMock()
        page.is_closed = MagicMock(return_value=False)
        page.wait_for_timeout = AsyncMock()
        bot._wait_spin_clear = AsyncMock(return_value=False)
        bot._is_spinning = AsyncMock(return_value=True)
        ready = AsyncMock(return_value=True)
        ok = await bot._wait_step_ready(
            page, ready, timeout_ms=200, label="s04身份证明"
        )
        self.assertFalse(ok)
        ready.assert_not_called()

    async def test_missing_step_overlay_is_loading_timeout(self):
        bot = IcrisRegistrationBot(llm=MagicMock())
        page = MagicMock()
        page.url = "https://example/s03.do"
        bot._is_spinning = AsyncMock(return_value=True)
        with self.assertRaises(IcrisLoadingTimeoutError) as ctx:
            await bot._raise_if_missing_registration_step(
                page, step="s04", label="身份证明页"
            )
        self.assertIn("s04", str(ctx.exception))
        self.assertIn("载入中超时", str(ctx.exception))

    async def test_missing_step_without_overlay_is_step_load(self):
        bot = IcrisRegistrationBot(llm=MagicMock())
        page = MagicMock()
        page.url = "https://example/s03.do"
        bot._is_spinning = AsyncMock(return_value=False)
        with self.assertRaises(IcrisStepLoadError) as ctx:
            await bot._raise_if_missing_registration_step(
                page, step="s04", label="身份证明页"
            )
        self.assertIn("未进入身份证明页", str(ctx.exception))

    async def test_wait_after_continue_raises_when_overlay_stuck(self):
        bot = IcrisRegistrationBot(llm=MagicMock())
        page = MagicMock()
        page.url = "https://example/s03.do"
        page.reload = AsyncMock()
        bot._wait_step_ready = AsyncMock(return_value=False)
        bot._get_validation_errors = AsyncMock(return_value=[])
        bot._wait_spin_clear = AsyncMock(return_value=False)
        with self.assertRaises(IcrisLoadingTimeoutError) as ctx:
            await bot._wait_after_continue(
                page, page.url, expect_step="s04"
            )
        self.assertIn("s04", str(ctx.exception))

    async def test_wait_after_continue_format_raises(self):
        bot = IcrisRegistrationBot(llm=MagicMock())
        page = MagicMock()
        page.url = "https://example/s04.do"
        bot._wait_step_ready = AsyncMock(return_value=False)
        bot._get_validation_errors = AsyncMock(
            return_value=["香港身分證號碼格式不正確！"]
        )
        bot._page_body_text = AsyncMock(return_value="")
        with self.assertRaises(IcrisFlowError) as ctx:
            await bot._wait_after_continue(
                page, page.url, expect_step="s04"
            )
        self.assertTrue(ctx.exception.no_requeue)
        self.assertFalse(ctx.exception.id_already_registered)
        self.assertIn("格式不", str(ctx.exception).replace(" ", ""))

    async def test_wait_after_continue_required_not_fatal(self):
        bot = IcrisRegistrationBot(llm=MagicMock())
        page = MagicMock()
        page.url = "https://example/s03.do"
        page.reload = AsyncMock()
        bot._wait_step_ready = AsyncMock(return_value=False)
        bot._get_validation_errors = AsyncMock(
            return_value=["請輸入身分證號碼"]
        )
        bot._page_body_text = AsyncMock(return_value="")
        ok = await bot._wait_after_continue(
            page, page.url, expect_step="s04"
        )
        self.assertFalse(ok)
        page.reload.assert_not_called()

    async def test_id_registered_raised_before_format(self):
        bot = IcrisRegistrationBot(llm=MagicMock())
        page = MagicMock()
        errs = [
            "相同的身分證件號碼已在系統中登記！",
            "香港身分證號碼格式不正確！",
        ]
        with self.assertRaises(IcrisFlowError) as ctx:
            await bot._raise_if_id_already_registered(page, errs)
            await bot._raise_if_format_invalid(page, errs, step="s04")
        self.assertTrue(ctx.exception.id_already_registered)
        self.assertTrue(ctx.exception.no_requeue)

    async def test_retry_s02_username_taken_raises_requeueable(self):
        bot = IcrisRegistrationBot(llm=MagicMock())
        page = MagicMock()
        data: dict = {"applicant": {"name_en": "Zhang San"}}
        bot._get_validation_errors = AsyncMock(
            return_value=["用户名称已存在"]
        )
        bot._regen_s02_username = MagicMock(return_value=("newu", "p@"))
        bot._fill_s02_credentials_fields = AsyncMock(return_value=3)
        bot._persist_s02_account_to_job = MagicMock()
        bot._click_account_profile_continue = AsyncMock(return_value=False)
        with self.assertRaises(IcrisFlowError) as ctx:
            await bot._retry_s02_if_username_taken(page, data)
        self.assertFalse(ctx.exception.no_requeue)
        self.assertIn("s02", str(ctx.exception))


class TestRegenS02Username(unittest.TestCase):
    def test_clears_session_and_rolls_suffix(self):
        bot = IcrisRegistrationBot(llm=MagicMock())
        data = {
            "applicant": {"name_en": "Zhang San", "id_number": "F570235(8)"},
            "_icris_session": {"username": "olduser", "password": "oldpass"},
        }
        user, _pwd = bot._regen_s02_username(data)
        self.assertNotEqual(user, "olduser")
        self.assertEqual(data["_icris_session"]["username"], user)
        self.assertNotEqual(data["icris_account"]["username"], "olduser")


class TestRegisterLoadingTimeoutWorker(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ExternalGroupStore(
            db_path=Path(self._tmp.name) / "reg_loading.db"
        )
        self.worker = IcrisJobWorker(store=self.store, workflow=MagicMock())
        self.worker.workflow.notify_job_result = MagicMock()
        self.worker._backoff_iso = lambda attempts: ""

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _claimed(self, room: str) -> tuple[int, dict]:
        job, created = self.store.enqueue_registration_job(room, source="test")
        self.assertTrue(created)
        claimed = self.store.claim_next_job()
        self.assertIsNotNone(claimed)
        return int(job["id"]), claimed

    def test_timeout_requeues_once_then_failed(self):
        self.worker.workflow.run_icris_job = MagicMock(
            side_effect=IcrisLoadingTimeoutError(
                register_loading_timeout_message("s04")
            )
        )
        job_id, claimed = self._claimed("room-to")
        self.assertEqual(int(claimed["attempts"]), 1)
        self.worker._process_job(claimed)
        row = self.store.get_registration_job(job_id)
        self.assertEqual(row["status"], "pending")
        self.assertIn("s04", row["last_error"])
        self.assertIn("载入中超时", row["last_error"])
        self.assertNotEqual(str(row.get("activation_status") or ""), "pending")

        claimed2 = self.store.claim_next_job()
        self.assertIsNotNone(claimed2)
        self.assertEqual(int(claimed2["id"]), job_id)
        self.assertEqual(int(claimed2["attempts"]), 2)
        self.worker._process_job(claimed2)
        row2 = self.store.get_registration_job(job_id)
        self.assertEqual(row2["status"], "failed")
        self.assertIn("载入中超时", row2["last_error"])
        self.worker.workflow.notify_job_result.assert_called()

    def test_step_load_still_requeues_under_max_attempts(self):
        self.worker.workflow.run_icris_job = MagicMock(
            side_effect=IcrisStepLoadError("未进入身份证明页: https://example")
        )
        job_id, claimed = self._claimed("room-step")
        self.worker._process_job(claimed)
        row = self.store.get_registration_job(job_id)
        self.assertEqual(row["status"], "pending")
        self.assertIn("未进入身份证明页", row["last_error"])

        claimed2 = self.store.claim_next_job()
        self.worker._process_job(claimed2)
        row2 = self.store.get_registration_job(job_id)
        self.assertEqual(row2["status"], "pending")
        self.assertEqual(int(row2["attempts"]), 2)

    def test_success_still_activation_pending(self):
        ctx = MagicMock()
        ctx.package_dir = ""
        ctx.messages = []
        ctx.esubmit_screenshot_path = ""
        ctx.success_screenshot_path = ""
        self.worker.workflow.run_icris_job = MagicMock(return_value=ctx)
        job_id, claimed = self._claimed("room-ok")
        self.worker._process_job(claimed)
        row = self.store.get_registration_job(job_id)
        self.assertEqual(row["status"], "succeeded")
        self.assertEqual(row["activation_status"], "pending")
        self.assertEqual(row["last_error"], "")

    def test_format_invalid_failed_writes_success_shot(self):
        shot = str(Path(self._tmp.name) / "fail.png")
        self.worker.workflow.run_icris_job = MagicMock(
            side_effect=IcrisFlowError(
                "s04 香港身分證號碼格式不正確！",
                no_requeue=True,
                screenshot_path=shot,
            )
        )
        job_id, claimed = self._claimed("room-fmt")
        self.worker._process_job(claimed)
        row = self.store.get_registration_job(job_id)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["screenshot_path"], shot)
        self.assertEqual(row["success_screenshot_path"], shot)
        self.assertIn("格式不", str(row["last_error"]).replace(" ", ""))
        self.assertEqual(int(row.get("id_already_registered") or 0), 0)
        self.assertNotEqual(str(row.get("activation_status") or ""), "pending")
        self.worker.workflow.notify_job_result.assert_called()

    def test_id_already_registered_still_flagged(self):
        shot = str(Path(self._tmp.name) / "dup.png")
        self.worker.workflow.run_icris_job = MagicMock(
            side_effect=IcrisFlowError(
                "相同的身分證件號碼已在系統中登記！",
                no_requeue=True,
                id_already_registered=True,
                screenshot_path=shot,
            )
        )
        job_id, claimed = self._claimed("room-idreg")
        self.worker._process_job(claimed)
        row = self.store.get_registration_job(job_id)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(int(row["id_already_registered"]), 1)
        self.assertEqual(row["success_screenshot_path"], shot)
        self.assertEqual(row["screenshot_path"], shot)

    def test_generic_error_failed_on_third_attempt(self):
        self.worker.workflow.run_icris_job = MagicMock(
            side_effect=IcrisStepLoadError("未进入身份证明页: https://example")
        )
        job_id, claimed = self._claimed("room-cap3")
        self.worker._process_job(claimed)
        self.assertEqual(
            self.store.get_registration_job(job_id)["status"], "pending"
        )
        claimed2 = self.store.claim_next_job()
        self.worker._process_job(claimed2)
        self.assertEqual(
            self.store.get_registration_job(job_id)["status"], "pending"
        )
        claimed3 = self.store.claim_next_job()
        self.assertIsNotNone(claimed3)
        self.assertEqual(int(claimed3["attempts"]), 3)
        self.worker._process_job(claimed3)
        row = self.store.get_registration_job(job_id)
        self.assertEqual(row["status"], "failed")
        self.assertIn("未进入身份证明页", row["last_error"])

    def test_failed_shot_fallback_fills_success_column(self):
        job_id, claimed = self._claimed("room-shotcol")
        self.store.mark_job_failed(
            job_id,
            error="s04 香港身分證號碼格式不正確！",
            screenshot_path="D:/fail.png",
        )
        row = self.store.get_registration_job(job_id)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["screenshot_path"], "D:/fail.png")
        self.assertEqual(row["success_screenshot_path"], "D:/fail.png")
        self.assertIn("格式不", str(row["last_error"]).replace(" ", ""))
