"""s03a 点红色「繼 續」：出错截图刷新再点一次；打回 s01 整段重跑；s05 等文案再截图。"""

from __future__ import annotations

import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

from src.browser.icris_errors import IcrisFlowError, IcrisRestartFromS01
from src.browser.icris_registration import CONTINUE_BTN_RE, IcrisRegistrationBot


class TestContinueButtonText(unittest.TestCase):
    def test_matches_spaced_traditional_and_simplified(self):
        self.assertTrue(CONTINUE_BTN_RE.search("繼 續"))
        self.assertTrue(CONTINUE_BTN_RE.search("繼續"))
        self.assertTrue(CONTINUE_BTN_RE.search("继 续"))
        self.assertTrue(CONTINUE_BTN_RE.search("继续"))

    def test_ignores_cancel_and_back(self):
        self.assertFalse(CONTINUE_BTN_RE.search("取 消"))
        self.assertFalse(CONTINUE_BTN_RE.search("返 回"))
        self.assertFalse(CONTINUE_BTN_RE.search("取消"))


class TestS03aNavToS05(unittest.IsolatedAsyncioTestCase):
    def _bot(self) -> IcrisRegistrationBot:
        return IcrisRegistrationBot(llm=MagicMock())

    def _ready_submit(self, bot: IcrisRegistrationBot, page: MagicMock) -> None:
        page.url = "https://example/registration/s03a.do"
        page.wait_for_timeout = AsyncMock()
        page.reload = AsyncMock()
        bot._is_esubmit_terms_step = AsyncMock(return_value=True)
        bot._wait_step_ready = AsyncMock(return_value=True)
        bot._esubmit_page_is_traditional = AsyncMock(return_value=True)
        bot._verify_esubmit_terms_checked = AsyncMock(
            return_value={
                "ok": True,
                "terms": True,
                "confirm": True,
                "found_terms": True,
                "found_confirm": True,
            }
        )
        bot._esubmit_terms_is_ready = AsyncMock(return_value=True)
        bot._log_page = AsyncMock()
        bot._save_error_screenshot = AsyncMock(return_value="x.png")
        bot._save_s03a_continue_error_screenshot = bot._save_error_screenshot
        bot._get_validation_errors = AsyncMock(return_value=["s$ is not defined"])
        bot._wait_spin_clear = AsyncMock(return_value=True)

    async def test_wait_after_continue_s05_does_not_reload(self):
        bot = self._bot()
        page = MagicMock()
        page.url = "https://example/registration/s03a.do"
        page.reload = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        bot._wait_step_ready = AsyncMock(return_value=False)
        bot._get_validation_errors = AsyncMock(return_value=[])
        bot._wait_spin_clear = AsyncMock(return_value=True)
        ok = await bot._wait_after_continue(page, page.url, expect_step="s05")
        self.assertFalse(ok)
        page.reload.assert_not_called()

    async def test_wait_after_continue_s03_no_err_does_not_reload(self):
        bot = self._bot()
        page = MagicMock()
        page.url = "https://example/registration/s03.do"
        page.reload = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        bot._wait_step_ready = AsyncMock(return_value=False)
        bot._get_validation_errors = AsyncMock(return_value=[])
        bot._wait_spin_clear = AsyncMock(return_value=True)
        ok = await bot._wait_after_continue(page, page.url, expect_step="s04")
        self.assertFalse(ok)
        page.reload.assert_not_called()

    async def test_wait_after_continue_s02_no_err_does_not_reload(self):
        bot = self._bot()
        page = MagicMock()
        page.url = "https://example/registration/s02.do"
        page.reload = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        bot._wait_step_ready = AsyncMock(return_value=False)
        bot._get_validation_errors = AsyncMock(return_value=[])
        bot._wait_spin_clear = AsyncMock(return_value=True)
        ok = await bot._wait_after_continue(page, page.url)
        self.assertFalse(ok)
        page.reload.assert_not_called()

    async def test_continue_nav_ok_s05_requires_success_step(self):
        bot = self._bot()
        page = MagicMock()
        page.is_closed = MagicMock(return_value=False)
        page.url = "https://example/registration/s03a.do"
        bot._is_home_or_portal = MagicMock(return_value=False)
        bot._is_spinning = AsyncMock(return_value=False)
        bot._is_success_step = AsyncMock(return_value=False)
        self.assertFalse(
            await bot._continue_nav_ok(
                page, page.url, want_s04=False, want_s05=True
            )
        )
        bot._is_success_step = AsyncMock(return_value=True)
        self.assertTrue(
            await bot._continue_nav_ok(
                page, page.url, want_s04=False, want_s05=True
            )
        )

    async def test_esubmit_terms_step_needs_copy_not_just_url(self):
        bot = self._bot()
        page = MagicMock()
        page.url = (
            "https://www.e-services.cr.gov.hk/ICRIS3EF/system/registration/s03a.do"
        )
        page.evaluate = AsyncMock(return_value=False)
        self.assertFalse(await bot._is_esubmit_terms_step(page))
        page.evaluate = AsyncMock(return_value=True)
        self.assertTrue(await bot._is_esubmit_terms_step(page))

    async def test_review_skips_when_already_approved(self):
        bot = self._bot()
        bot.job_id = 62
        bot.on_review_needed = MagicMock()
        page = MagicMock()
        page.wait_for_timeout = AsyncMock()
        with patch("src.storage.db.ExternalGroupStore") as store_cls:
            store = store_cls.return_value
            store.get_job_review_status.return_value = "approved"
            ok = await bot._wait_for_review_approval(page)
        self.assertTrue(ok)
        store.mark_job_awaiting_review.assert_not_called()
        bot.on_review_needed.assert_not_called()

    async def test_s03a_handler_skips_second_pass(self):
        bot = self._bot()
        bot._s03a_handled = True
        bot._wait_for_review_approval = AsyncMock()
        bot._accept_esubmit_terms = AsyncMock()
        bot._is_esubmit_terms_step = AsyncMock(return_value=True)
        await bot._run_s03a_review_and_submit(MagicMock())
        bot._wait_for_review_approval.assert_not_called()
        bot._accept_esubmit_terms.assert_not_called()

    async def test_accept_esubmit_clicks_continue_expect_s05(self):
        bot = self._bot()
        page = MagicMock()
        self._ready_submit(bot, page)
        bot._is_success_step = AsyncMock(side_effect=[False, True])
        bot._click_continue = AsyncMock(return_value=True)
        ok = await bot._accept_esubmit_terms(page, submit=True)
        self.assertTrue(ok)
        bot._click_continue.assert_awaited_with(page, expect_step="s05")
        self.assertEqual(bot._click_continue.await_count, 1)
        page.reload.assert_not_called()

    async def test_accept_esubmit_error_reloads_then_fails_on_second(self):
        bot = self._bot()
        page = MagicMock()
        self._ready_submit(bot, page)
        bot._is_success_step = AsyncMock(return_value=False)
        bot._click_continue = AsyncMock(return_value=False)
        with self.assertRaises(IcrisRestartFromS01) as ctx:
            await bot._accept_esubmit_terms(page, submit=True)
        self.assertIn("s$ is not defined", str(ctx.exception))
        page.reload.assert_awaited()
        self.assertEqual(bot._click_continue.await_count, 2)
        bot._save_error_screenshot.assert_awaited()

    async def test_accept_esubmit_reload_to_s01_raises_restart(self):
        bot = self._bot()
        page = MagicMock()
        self._ready_submit(bot, page)
        bot._click_continue = AsyncMock(return_value=False)
        bot._s03a_post_continue_where = AsyncMock(
            side_effect=["s03a", "s03a", "s01"]
        )
        with self.assertRaises(IcrisRestartFromS01) as ctx:
            await bot._accept_esubmit_terms(page, submit=True)
        self.assertIn("s01", str(ctx.exception))
        page.reload.assert_awaited()
        self.assertEqual(bot._click_continue.await_count, 1)

    async def test_accept_esubmit_leave_s03a_not_s05_raises_restart(self):
        bot = self._bot()
        page = MagicMock()
        self._ready_submit(bot, page)
        bot._click_continue = AsyncMock(return_value=False)
        bot._s03a_post_continue_where = AsyncMock(
            side_effect=["s03a", "s03a", "left"]
        )
        with self.assertRaises(IcrisRestartFromS01) as ctx:
            await bot._accept_esubmit_terms(page, submit=True)
        self.assertIn("left", str(ctx.exception))
        page.reload.assert_awaited()
        self.assertEqual(bot._click_continue.await_count, 1)

    async def test_run_attempt_reruns_once_from_s01(self):
        bot = self._bot()
        page = MagicMock()
        page.is_closed = MagicMock(return_value=False)
        page.close = AsyncMock()
        new_page = MagicMock()
        page.context.new_page = AsyncMock(return_value=new_page)
        data: dict = {}
        bot._reset_flow_flags = MagicMock()
        bot._regen_s02_username = MagicMock(return_value=("u2", "p2"))
        bot._persist_s02_account_to_job = MagicMock()
        bot._save_error_screenshot = AsyncMock(return_value="x.png")
        bot._run_registration_attempt = AsyncMock(
            side_effect=[IcrisRestartFromS01("s01"), new_page]
        )
        out = await bot._run_attempt_with_s03a_rerun(page, data)
        self.assertIs(out, new_page)
        self.assertEqual(bot._run_registration_attempt.await_count, 2)
        self.assertEqual(bot._reset_flow_flags.call_count, 2)
        bot._regen_s02_username.assert_called_once()
        page.close.assert_awaited()
        second_page = bot._run_registration_attempt.await_args_list[1].args[0]
        self.assertIs(second_page, new_page)

    async def test_run_attempt_second_restart_fails(self):
        bot = self._bot()
        page = MagicMock()
        page.is_closed = MagicMock(return_value=False)
        page.close = AsyncMock()
        new_page = MagicMock()
        page.context.new_page = AsyncMock(return_value=new_page)
        bot._reset_flow_flags = MagicMock()
        bot._regen_s02_username = MagicMock(return_value=("u2", "p2"))
        bot._persist_s02_account_to_job = MagicMock()
        bot.error_screenshot_path = "/tmp/alert.png"
        bot._run_registration_attempt = AsyncMock(
            side_effect=[
                IcrisRestartFromS01("s01"),
                IcrisRestartFromS01("left"),
            ]
        )
        with self.assertRaises(IcrisFlowError) as ctx:
            await bot._run_attempt_with_s03a_rerun(page, {})
        self.assertTrue(ctx.exception.no_requeue)
        self.assertEqual(ctx.exception.screenshot_path, "/tmp/alert.png")
        self.assertIn("已整段重跑一次", str(ctx.exception))
        self.assertEqual(bot._run_registration_attempt.await_count, 2)
        page.close.assert_awaited()

    async def test_restart_carries_screenshot_path(self):
        bot = self._bot()
        page = MagicMock()
        page.url = "https://example/registration/s03.do"
        bot._page_body_text = AsyncMock(return_value="")
        bot._save_error_screenshot = AsyncMock(return_value="x.png")
        with self.assertRaises(IcrisRestartFromS01) as ctx:
            await bot._raise_if_rerun_on_page_error(
                page, ["s$ is not defined"], step="s03"
            )
        self.assertEqual(ctx.exception.screenshot_path, "x.png")

    async def test_username_taken_body_fallback_no_restart(self):
        bot = self._bot()
        page = MagicMock()
        page.url = "https://example/registration/s02.do"
        bot._get_validation_errors = AsyncMock(return_value=[])
        bot._page_body_text = AsyncMock(
            return_value="账户资料 用户名称已存在，请更换"
        )
        bot._save_error_screenshot = AsyncMock(return_value="x.png")
        await bot._raise_if_rerun_on_page_error(page, [], step="s02")
        bot._save_error_screenshot.assert_not_awaited()

    async def test_format_invalid_body_fallback_raises(self):
        bot = self._bot()
        page = MagicMock()
        page.url = "https://example/registration/s04.do"
        bot._get_validation_errors = AsyncMock(return_value=[])
        bot._page_body_text = AsyncMock(
            return_value="身分證明 香港身分證號碼格式不正確！"
        )
        with self.assertRaises(IcrisFlowError) as ctx:
            await bot._raise_if_rerun_on_page_error(page, [], step="s04")
        self.assertTrue(ctx.exception.no_requeue)

    async def test_id_registered_not_restart(self):
        bot = self._bot()
        page = MagicMock()
        page.url = "https://example/registration/s04.do"
        bot._page_body_text = AsyncMock(return_value="")
        bot._save_error_screenshot = AsyncMock(return_value="x.png")
        with self.assertRaises(IcrisFlowError) as ctx:
            await bot._raise_if_rerun_on_page_error(
                page, ["相同的身分證件號碼已在系統中登記！"], step="s04"
            )
        self.assertTrue(ctx.exception.id_already_registered)
        self.assertTrue(ctx.exception.no_requeue)
        self.assertEqual(bot.error_screenshot_path, "")

    async def test_get_validation_errors_scrapes_ant_alert(self):
        bot = self._bot()
        page = MagicMock()
        captured: dict[str, str] = {}

        async def evaluate(js: str) -> list[str]:
            captured["js"] = js
            return ["s$ is not defined"]

        page.evaluate = evaluate
        errs = await bot._get_validation_errors(page)
        self.assertEqual(errs, ["s$ is not defined"])
        self.assertIn("ant-alert", captured["js"])
        self.assertIn("role=alert", captured["js"])
        self.assertIn("anticon-close-circle", captured["js"])
        self.assertIn("ant-form-item-has-error", captured["js"])

    async def test_success_step_needs_copy_not_url(self):
        bot = self._bot()
        page = MagicMock()
        page.url = "https://example/registration/s05.do"
        page.evaluate = AsyncMock(return_value=False)
        self.assertFalse(await bot._is_success_step(page))
        page.evaluate = AsyncMock(return_value=True)
        self.assertTrue(await bot._is_success_step(page))

    async def test_save_success_screenshot_skips_without_copy(self):
        bot = self._bot()
        page = MagicMock()
        page.url = "https://example/registration/s05.do"
        page.is_closed = MagicMock(return_value=False)
        page.screenshot = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        bot._is_spinning = AsyncMock(return_value=False)
        bot._wait_spin_clear = AsyncMock(return_value=True)
        bot._dismiss_cookie_banner = AsyncMock()
        bot._wait_step_ready = AsyncMock(return_value=False)
        await bot._save_success_screenshot(page)
        page.screenshot.assert_not_called()
        self.assertEqual(bot.success_screenshot_path, "")

    async def test_click_continue_s05_does_not_scan_other_selectors(self):
        bot = self._bot()
        page = MagicMock()
        page.url = "https://example/registration/s03a.do"
        page.wait_for_timeout = AsyncMock()
        page.get_by_role = MagicMock()
        page.evaluate = AsyncMock(return_value=False)
        bot._wait_spin_clear = AsyncMock(return_value=True)
        bot._wait_after_continue = AsyncMock(return_value=False)
        bot._is_home_or_portal = MagicMock(return_value=False)

        danger_btn = MagicMock()
        danger_btn.count = AsyncMock(return_value=1)
        danger_btn.is_visible = AsyncMock(return_value=True)
        danger_btn.inner_text = AsyncMock(return_value="繼 續")
        danger_btn.is_disabled = AsyncMock(return_value=False)
        danger_btn.scroll_into_view_if_needed = AsyncMock()
        danger_btn.click = AsyncMock()
        danger_btn.get_attribute = AsyncMock(return_value="")

        locators: list[str] = []

        def locator(sel: str):
            locators.append(sel)
            mock = MagicMock()
            mock.last = danger_btn
            mock.first = MagicMock()
            mock.first.count = AsyncMock(return_value=0)
            return mock

        page.locator = locator
        ok = await bot._click_continue(page, expect_step="s05")
        self.assertFalse(ok)
        self.assertTrue(locators)
        self.assertTrue(all("ant-btn-danger" in sel for sel in locators))
        page.get_by_role.assert_not_called()
        danger_btn.click.assert_awaited()

    async def test_wait_after_continue_s04_alert_restarts(self):
        bot = self._bot()
        page = MagicMock()
        page.url = "https://example/registration/s04.do"
        page.reload = AsyncMock()
        bot._wait_step_ready = AsyncMock(return_value=False)
        bot._get_validation_errors = AsyncMock(return_value=["s$ is not defined"])
        bot._page_body_text = AsyncMock(return_value="")
        bot._save_error_screenshot = AsyncMock(return_value="x.png")
        with self.assertRaises(IcrisRestartFromS01) as ctx:
            await bot._wait_after_continue(page, page.url, expect_step="s03a")
        self.assertIn("s$ is not defined", str(ctx.exception))
        page.reload.assert_not_called()
        bot._save_error_screenshot.assert_awaited()
        self.assertEqual(bot.error_screenshot_path, "x.png")

    async def test_wait_after_continue_username_taken_no_restart(self):
        bot = self._bot()
        page = MagicMock()
        page.url = "https://example/registration/s02.do"
        page.reload = AsyncMock()
        bot._wait_step_ready = AsyncMock(return_value=False)
        bot._get_validation_errors = AsyncMock(return_value=["用户名称已存在"])
        bot._page_body_text = AsyncMock(return_value="")
        bot._save_error_screenshot = AsyncMock()
        ok = await bot._wait_after_continue(page, page.url)
        self.assertFalse(ok)
        page.reload.assert_not_called()
        bot._save_error_screenshot.assert_not_awaited()

    async def test_prepare_rerun_clears_review_and_resets_s03a(self):
        bot = self._bot()
        bot.job_id = 7
        bot.esubmit_screenshot_path = "old.png"
        bot._s03a_handled = True
        bot._user_info_filled = True
        bot._regen_s02_username = MagicMock(return_value=("u2", "p2"))
        bot._persist_s02_account_to_job = MagicMock()
        data = {"icris_account": {}}
        with patch("src.storage.db.ExternalGroupStore") as store_cls:
            store = store_cls.return_value
            bot._prepare_registration_rerun(data)
        self.assertEqual(bot.esubmit_screenshot_path, "")
        self.assertFalse(bot._s03a_handled)
        self.assertTrue(bot._registration_rerun)
        store.reset_job_review_for_rerun.assert_called_once_with(7)
        bot._regen_s02_username.assert_called_once_with(data)
        bot._persist_s02_account_to_job.assert_called_once_with(data)

    async def test_rerun_review_does_not_skip_when_review_cleared(self):
        bot = self._bot()
        bot.job_id = 8
        bot.on_review_needed = MagicMock()
        page = MagicMock()
        page.wait_for_timeout = AsyncMock()
        with patch("src.storage.db.ExternalGroupStore") as store_cls:
            store = store_cls.return_value
            store.get_job_review_status.side_effect = ["", "approved"]
            store.get_job_status.return_value = "awaiting_review"
            ok = await bot._wait_for_review_approval(page)
        self.assertTrue(ok)
        store.mark_job_awaiting_review.assert_called_once_with(8)
        bot.on_review_needed.assert_called_once()

    def test_s03_user_info_url_excludes_s03a(self):
        bot = self._bot()
        self.assertTrue(
            bot._is_s03_user_info_url(
                "https://example/system/registration/s03.do"
            )
        )
        self.assertFalse(
            bot._is_s03_user_info_url(
                "https://example/system/registration/s03a.do"
            )
        )
        self.assertTrue(
            bot._is_s02_url("https://example/system/registration/s02.do")
        )

    async def test_advance_s03_alert_restarts_without_goto_s04(self):
        bot = self._bot()
        page = MagicMock()
        page.url = "https://example/registration/s03.do"
        page.reload = AsyncMock()
        page.goto = AsyncMock()
        page.evaluate = AsyncMock()
        bot._is_identity_proof_step = AsyncMock(return_value=False)
        bot._is_user_info_step = AsyncMock(return_value=True)
        bot._wait_spin_clear = AsyncMock(return_value=True)
        bot._click_continue = AsyncMock(return_value=False)
        bot._get_validation_errors = AsyncMock(return_value=["s$ is not defined"])
        bot._page_body_text = AsyncMock(return_value="")
        bot._save_error_screenshot = AsyncMock(return_value="x.png")
        with self.assertRaises(IcrisRestartFromS01) as ctx:
            await bot._advance_from_user_info_to_identity(page)
        self.assertIn("s$ is not defined", str(ctx.exception))
        page.reload.assert_not_called()
        page.goto.assert_not_called()
        self.assertIn("s03", page.url)
        self.assertNotIn("s04", page.url)

    async def test_advance_s03_stuck_restarts_without_reload(self):
        bot = self._bot()
        page = MagicMock()
        page.url = "https://example/registration/s03.do"
        page.reload = AsyncMock()
        page.goto = AsyncMock()
        page.evaluate = AsyncMock()
        bot._is_identity_proof_step = AsyncMock(return_value=False)
        bot._is_user_info_step = AsyncMock(return_value=True)
        bot._wait_spin_clear = AsyncMock(return_value=True)
        bot._click_continue = AsyncMock(return_value=False)
        bot._get_validation_errors = AsyncMock(return_value=[])
        bot._is_home_or_portal = MagicMock(return_value=False)
        bot._save_error_screenshot = AsyncMock(return_value="x.png")
        with self.assertRaises(IcrisRestartFromS01) as ctx:
            await bot._advance_from_user_info_to_identity(page)
        self.assertIn("未进入 s04", str(ctx.exception))
        page.reload.assert_not_called()
        page.goto.assert_not_called()
        bot._save_error_screenshot.assert_awaited()
        self.assertNotIn("s04.do", page.url)

    def _patch_browser_stack(self, page: MagicMock):
        """mock run() 的 playwright/浏览器栈，返回 (browser, patchers)。"""
        browser = MagicMock()
        browser.close = AsyncMock()
        browser.contexts = [MagicMock()]
        context = MagicMock()
        context.new_page = AsyncMock(return_value=page)
        pw_cm = MagicMock()
        pw_cm.__aenter__ = AsyncMock(return_value=MagicMock())
        pw_cm.__aexit__ = AsyncMock(return_value=False)
        pw_factory = MagicMock(return_value=pw_cm)
        patchers = [
            patch(
                "src.browser.launcher.import_async_playwright",
                return_value=pw_factory,
            ),
            patch(
                "src.browser.icris_registration.launch_browser",
                new=AsyncMock(return_value=browser),
            ),
            patch(
                "src.browser.icris_registration.create_browser_context",
                new=AsyncMock(return_value=context),
            ),
            patch("src.browser.icris_registration.settings"),
        ]
        return browser, patchers

    async def test_run_closes_browser_on_success(self):
        bot = self._bot()
        page = MagicMock()
        page.is_closed = MagicMock(return_value=False)
        page.wait_for_timeout = AsyncMock()
        browser, patchers = self._patch_browser_stack(page)
        bot._run_attempt_with_s03a_rerun = AsyncMock(return_value=page)
        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in patchers]
            mocks[3].browser_keep_open_seconds = 0
            await bot.run({})
        browser.close.assert_awaited()

    async def test_run_closes_browser_on_failure(self):
        bot = self._bot()
        page = MagicMock()
        page.is_closed = MagicMock(return_value=True)
        browser, patchers = self._patch_browser_stack(page)
        bot._run_attempt_with_s03a_rerun = AsyncMock(
            side_effect=IcrisFlowError(
                "页面报错", no_requeue=True, screenshot_path="x.png"
            )
        )
        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in patchers]
            mocks[3].browser_keep_open_seconds = 0
            with self.assertRaises(IcrisFlowError):
                await bot.run({})
        browser.close.assert_awaited()


if __name__ == "__main__":
    unittest.main()
