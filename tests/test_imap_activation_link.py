import unittest

from src.email.imap_client import (
    extract_activation_link_from_body,
    imap_search_queries,
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
        self.assertEqual(
            extract_activation_link_from_body(html),
            "https://www.e-services.cr.gov.hk/corp/web/token?id=abc123",
        )

    def test_extract_html_entity_amp(self):
        html = (
            '<a href="https://www.e-services.cr.gov.hk/path?a=1&amp;b=2">x</a>'
        )
        link = extract_activation_link_from_body(html)
        self.assertIsNotNone(link)
        assert link is not None
        self.assertIn("e-services.cr.gov.hk/path", link)
        self.assertIn("b=2", link)

    def test_extract_plain_activate_keyword(self):
        body = "Please visit https://example.com/user/activate?x=1 now"
        self.assertEqual(
            extract_activation_link_from_body(body),
            "https://example.com/user/activate?x=1",
        )

    def test_extract_ignores_eservices_homepage(self):
        html = '<a href="https://www.e-services.cr.gov.hk/">Home</a>'
        self.assertIsNone(extract_activation_link_from_body(html))

    def test_is_activation_url_cr_domain_path(self):
        self.assertTrue(
            is_activation_url(
                "https://www.e-services.cr.gov.hk/corp/account/start"
            )
        )
        self.assertFalse(is_activation_url("https://www.cr.gov.hk/"))
        self.assertFalse(is_activation_url("not-a-url"))

    def test_is_activation_url_s06_code(self):
        self.assertTrue(is_activation_url(_S06_URL))

    def test_prefers_s06_over_other_eservices_link(self):
        html = f"""
        <a href="https://www.e-services.cr.gov.hk/corp/web/token?id=other">other</a>
        <a href="{_S06_URL}">activate</a>
        """
        self.assertEqual(extract_activation_link_from_body(html), _S06_URL)

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
        for charset, criteria in queries:
            self.assertIsNone(charset)
            self.assertNotIn(" OR ", criteria)
            self.assertIn("SINCE 01-Jan-2026", criteria)
            self.assertTrue(criteria.startswith("(") and criteria.endswith(")"))


if __name__ == "__main__":
    unittest.main()
