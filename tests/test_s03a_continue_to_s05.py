"""s03a 点红色「繼 續」后必须进入 s05，禁止 reload 与重复审核。"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.browser.icris_errors import IcrisStepLoadError
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
        page.url = "https://example/registration/s03a.do"
        page.wait_for_timeout = AsyncMock()
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
        bot._is_success_step = AsyncMock(side_effect=[False, True])
        bot._click_continue = AsyncMock(return_value=True)
        bot._log_page = AsyncMock()
        ok = await bot._accept_esubmit_terms(page, submit=True)
        self.assertTrue(ok)
        bot._click_continue.assert_awaited_with(page, expect_step="s05")

    async def test_accept_esubmit_raises_if_not_s05(self):
        bot = self._bot()
        page = MagicMock()
        page.url = "https://example/registration/s03a.do#"
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
        bot._is_success_step = AsyncMock(return_value=False)
        bot._click_continue = AsyncMock(return_value=False)
        bot._log_page = AsyncMock()
        with self.assertRaises(IcrisStepLoadError) as ctx:
            await bot._accept_esubmit_terms(page, submit=True)
        self.assertIn("未进入提交成功页", str(ctx.exception))
        page.reload.assert_not_called()
        self.assertEqual(bot._click_continue.await_count, 2)


if __name__ == "__main__":
    unittest.main()
