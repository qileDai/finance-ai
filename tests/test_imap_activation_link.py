import unittest

from src.email.imap_client import (
    extract_activation_link_from_body,
    imap_search_queries,
    is_activation_url,
)


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
