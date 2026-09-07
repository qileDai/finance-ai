import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from src.browser.icris_activation import (
    _click_s06_confirm,
    _page_looks_activated,
    _wait_activation_result,
    fill_s06_activation_form,
    normalize_s06_activation_url,
    require_s06_activation_url,
    s06_credential_error,
)


class _Locator:
    def __init__(self, *, visible=True, value=""):
        self.visible = visible
        self.value = value
        self.clicked = False
        self.filled = None
        self.events = []

    @property
    def first(self):
        return self

    async def count(self):
        return 1 if self.visible else 0

    async def is_visible(self):
        return self.visible

    async def scroll_into_view_if_needed(self):
        return None

    async def click(self, **_kwargs):
        self.clicked = True

    async def fill(self, value):
        self.filled = value
        self.value = value

    async def dispatch_event(self, name):
        self.events.append(name)

    async def input_value(self):
        return self.value


class _Page:
    def __init__(self):
        self.user = _Locator()
        self.password = _Locator()
        self.confirm = _Locator()
        self.evaluated = None

    def locator(self, sel):
        if sel in ("#userId", "input[placeholder*='User ID' i]"):
            return self.user
        if sel in ("#password", "input[type='password']"):
            return self.password
        return _Locator(visible=False)

    def get_by_role(self, role, name=None):
        self.confirm_role = role
        self.confirm_name = name
        return self.confirm

    async def evaluate(self, script):
        self.evaluated = script
        return False


class TestNormalizeS06Url(unittest.TestCase):
    def test_rewrites_ep_to_ef(self):
        src = (
            "https://www.e-services.cr.gov.hk/ICRIS3EP/system/"
            "registration/s06.do?code=DQXAHYBCTKKY"
        )
        out = normalize_s06_activation_url(src)
        self.assertIn("/ICRIS3EF/", out)
        self.assertNotIn("/ICRIS3EP/", out)
        self.assertIn("code=DQXAHYBCTKKY", out)

    def test_keeps_ef(self):
        src = (
            "https://e-services.cr.gov.hk/ICRIS3EF/system/"
            "registration/s06.do?code=ABC"
        )
        self.assertEqual(normalize_s06_activation_url(src), src)

    def test_empty(self):
        self.assertEqual(normalize_s06_activation_url(""), "")
        self.assertEqual(normalize_s06_activation_url("  "), "")

    def test_require_rejects_home_do(self):
        url, err = require_s06_activation_url(
            "https://www.e-services.cr.gov.hk/ICRIS3EF/system/home.do"
        )
        self.assertTrue(err)
        self.assertIn("不是启动帐户链接", err)

    def test_require_accepts_s06(self):
        src = (
            "https://www.e-services.cr.gov.hk/ICRIS3EP/system/"
            "registration/s06.do?code=DQXAHYBCTKKY"
        )
        url, err = require_s06_activation_url(src)
        self.assertEqual(err, "")
        self.assertIn("/ICRIS3EF/", url)
        self.assertIn("s06.do", url)


class TestS06CredentialError(unittest.TestCase):
    def test_english_banner(self):
        body = (
            "Incorrect User ID or Password "
            "[For incorrect password, account will be locked after 5 unsuccessful attempts]"
        )
        msg = s06_credential_error(body)
        self.assertIn("不正确", msg)
        self.assertIn("5", msg)

    def test_chinese(self):
        self.assertIn("不正确", s06_credential_error("用戶名稱或密碼不正確"))

    def test_success_not_error(self):
        self.assertEqual(s06_credential_error("Account activation successful"), "")


class TestFillS06Form(unittest.TestCase):
    def test_fills_userid_password_and_clicks_confirm(self):
        page = _Page()

        async def _run():
            await fill_s06_activation_form(page, "DQL551172YT", "SecretPass1@")

        asyncio.run(_run())
        self.assertEqual(page.user.filled, "DQL551172YT")
        self.assertEqual(page.password.filled, "SecretPass1@")
        self.assertTrue(page.confirm.clicked)
        self.assertEqual(page.confirm_role, "button")
        self.assertTrue(page.confirm_name.search("Confirm"))
        self.assertTrue(page.confirm_name.search("确认"))

    def test_requires_credentials(self):
        page = _Page()
        with self.assertRaises(ValueError):
            asyncio.run(fill_s06_activation_form(page, "", "x"))

    def test_confirm_falls_back_to_evaluate(self):
        page = _Page()
        page.confirm.visible = False
        page.evaluate = AsyncMock(return_value=True)

        async def _run():
            self.assertTrue(await _click_s06_confirm(page))

        asyncio.run(_run())
        page.evaluate.assert_awaited()


class TestPageLooksActivated(unittest.TestCase):
    def test_incorrect_password_not_success(self):
        page = AsyncMock()
        page.inner_text = AsyncMock(
            return_value=(
                "Incorrect User ID or Password "
                "[For incorrect password, account will be locked "
                "after 5 unsuccessful attempts]"
            )
        )
        page.url = (
            "https://e-services.cr.gov.hk/ICRIS3EF/system/"
            "registration/s06.do?code=x"
        )
        self.assertFalse(
            asyncio.run(_page_looks_activated(page, after_submit=True))
        )


class _WaitPage:
    def __init__(self, body: str, url: str = ""):
        self._body = body
        self.url = url or (
            "https://e-services.cr.gov.hk/ICRIS3EF/system/"
            "registration/s06.do?code=x"
        )

    async def inner_text(self, _sel):
        return self._body

    async def wait_for_timeout(self, _ms):
        return None


class TestWaitActivationResult(unittest.TestCase):
    def test_credential_error_returns_immediately(self):
        page = _WaitPage(
            "Incorrect User ID or Password "
            "[For incorrect password, account will be locked "
            "after 5 unsuccessful attempts]"
        )

        async def _run():
            with patch(
                "src.browser.icris_ui_common.wait_spin_clear",
                new_callable=AsyncMock,
            ):
                return await _wait_activation_result(page, timeout_ms=5000)

        status, detail = asyncio.run(_run())
        self.assertEqual(status, "credential_error")
        self.assertIn("不正确", detail)

    def test_success_keyword_stops_waiting(self):
        page = _WaitPage(
            "Account activation successful",
            url="https://e-services.cr.gov.hk/ICRIS3EF/system/home.do",
        )

        async def _run():
            with patch(
                "src.browser.icris_ui_common.wait_spin_clear",
                new_callable=AsyncMock,
            ):
                return await _wait_activation_result(page, timeout_ms=5000)

        status, detail = asyncio.run(_run())
        self.assertEqual(status, "success")
        self.assertEqual(detail, "")
