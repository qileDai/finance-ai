import unittest

from src.email.imap_client import (
    extract_activation_link_from_body,
    imap_search_queries,
    is_activation_subject,
    is_activation_url,
    parse_activation_email,
    select_activation_link,
)

_S06_URL = (
    "https://www.e-services.cr.gov.hk/ICRIS3EF/system/registration/s06.do?code=AbC123"
)

_REAL_MAIL_HTML = f"""
<html><body>
<p>電子服務 - 用戶登記及啟動</p>
<p>用戶名稱：MAWADA123</p>
<p>請按下列連結啟動您的帳戶：</p>
<a href="{_S06_URL}">啟動帳戶</a>
</body></html>
"""


class TestImapActivationLink(unittest.TestCase):
    def test_extract_href_eservices_without_activate_keyword(self):
        html = """
        <html><body>
        <p>Please click the link below to continue.</p>
        <a href="https://www.e-services.cr.gov.hk/corp/web/token?id=abc123">Open</a>
        </body></html>
        """
        self.assertIsNone(extract_activation_link_from_body(html))

    def test_extract_html_entity_amp(self):
        html = (
            '<a href="https://www.e-services.cr.gov.hk/ICRIS3EF/'
            'system/registration/s06.do?code=AbC&amp;x=1">x</a>'
        )
        link = extract_activation_link_from_body(html)
        self.assertIsNotNone(link)
        assert link is not None
        self.assertIn("s06.do", link)
        self.assertIn("code=", link)

    def test_extract_plain_activate_keyword(self):
        body = "Please visit https://example.com/user/activate?x=1 now"
        self.assertIsNone(extract_activation_link_from_body(body))

    def test_extract_ignores_eservices_homepage(self):
        html = '<a href="https://www.e-services.cr.gov.hk/">Home</a>'
        self.assertIsNone(extract_activation_link_from_body(html))

    def test_rejects_home_do(self):
        home = "https://www.e-services.cr.gov.hk/ICRIS3EF/system/home.do"
        self.assertFalse(is_activation_url(home))
        html = f'<a href="{home}">Home</a><p>footer</p>'
        self.assertIsNone(extract_activation_link_from_body(html))

    def test_is_activation_url_cr_domain_path(self):
        self.assertFalse(
            is_activation_url(
                "https://www.e-services.cr.gov.hk/corp/account/start"
            )
        )
        self.assertFalse(is_activation_url("https://www.cr.gov.hk/"))
        self.assertFalse(is_activation_url("not-a-url"))

    def test_is_activation_url_s06_code(self):
        self.assertTrue(is_activation_url(_S06_URL))

    def test_prefers_s06_over_home_do(self):
        html = f"""
        <a href="https://www.e-services.cr.gov.hk/ICRIS3EF/system/home.do">home</a>
        <a href="{_S06_URL}">activate</a>
        """
        self.assertEqual(extract_activation_link_from_body(html), _S06_URL)

    def test_extract_s06_split_by_br(self):
        html = (
            "https://www.e-services.cr.gov.hk/ICRIS3EP/system/registration/"
            "<br>s06.do?code=AbC123"
        )
        link = extract_activation_link_from_body(html)
        self.assertIsNotNone(link)
        assert link is not None
        self.assertIn("s06.do", link)
        self.assertIn("code=AbC123", link)
        self.assertIn("/ICRIS3EF/", link)

    def test_parse_real_activation_mail(self):
        parsed = parse_activation_email(_REAL_MAIL_HTML)
        self.assertEqual(parsed.get("url"), _S06_URL)
        self.assertEqual(parsed.get("username"), "MAWADA123")

    def test_parse_username_from_table_html(self):
        html = f"""
        <table>
          <tr><td>用戶名稱</td><td>MAWADA123</td></tr>
        </table>
        <a href="{_S06_URL}">go</a>
        """
        parsed = parse_activation_email(html)
        self.assertEqual(parsed.get("username"), "MAWADA123")
        self.assertEqual(parsed.get("url"), _S06_URL)

    def test_select_link_matches_username_case_insensitive(self):
        self.assertEqual(
            select_activation_link(_REAL_MAIL_HTML, "mawada123"),
            _S06_URL,
        )

    def test_select_link_rejects_other_username(self):
        self.assertIsNone(select_activation_link(_REAL_MAIL_HTML, "OTHERUSER"))

    def test_select_link_rejects_missing_username(self):
        html = f'<a href="{_S06_URL}">啟動</a>'
        self.assertIsNone(select_activation_link(html, "MAWADA123"))

    def test_select_link_rejects_empty_expected(self):
        self.assertIsNone(select_activation_link(_REAL_MAIL_HTML, ""))

    def test_imap_search_queries_are_legal(self):
        queries = imap_search_queries("01-Jan-2026")
        self.assertTrue(queries)
        subjects = [c for _, c in queries]
        self.assertTrue(any("用戶登記及啟動" in c for c in subjects))
        for charset, criteria in queries:
            self.assertIn(charset, (None, "UTF-8"))
            if "用戶登記及啟動" in criteria or "用户登记及启动" in criteria:
                self.assertEqual(charset, "UTF-8")
            self.assertNotIn(" OR ", criteria)
            self.assertIn("SINCE 01-Jan-2026", criteria)
            self.assertTrue(criteria.startswith("(") and criteria.endswith(")"))

    def test_activation_subject_matches_real_title(self):
        self.assertTrue(is_activation_subject("電子服務 - 用戶登記及啟動"))
        self.assertTrue(is_activation_subject("电子服务 - 用户登记及启动"))
        self.assertFalse(is_activation_subject("開始提供服務通知 (電子提交)"))
        self.assertFalse(is_activation_subject("New device login reminder"))


if __name__ == "__main__":
    unittest.main()
