import tempfile
import unittest
from pathlib import Path

from src.storage.db import ExternalGroupStore
from src.web.admin_api import _handle_email_account_upsert, handle_admin_api


class TestEmailAccountUpsert(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ExternalGroupStore(
            db_path=Path(self._tmp.name) / "test_email.db"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_empty_body_returns_400(self):
        data, code = _handle_email_account_upsert(self.store, {})
        self.assertEqual(code, 400)
        self.assertFalse(data.get("ok"))

    def test_save_and_lookup_case_insensitive(self):
        data, code = _handle_email_account_upsert(
            self.store,
            {
                "email_address": "Verify-Save-Test@163.com",
                "imap_host": "imap.163.com",
                "imap_port": 993,
                "username": "Verify-Save-Test@163.com",
                "password": "auth-code",
                "label": "save-verify",
            },
        )
        self.assertEqual(code, 200, data)
        account = data["account"]
        self.assertEqual(account["email_address"], "verify-save-test@163.com")
        found = self.store.get_email_account_by_address(
            "VERIFY-SAVE-TEST@163.com"
        )
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], account["id"])

    def test_edit_by_id_keeps_row(self):
        _, code = _handle_email_account_upsert(
            self.store,
            {
                "email_address": "a@163.com",
                "imap_host": "imap.163.com",
                "username": "a@163.com",
                "password": "p1",
            },
        )
        self.assertEqual(code, 200)
        first = self.store.list_email_accounts()[0]
        data, code = _handle_email_account_upsert(
            self.store,
            {
                "id": first["id"],
                "email_address": "b@163.com",
                "imap_host": "imap.163.com",
                "username": "b@163.com",
                "password": "",
                "label": "renamed",
            },
        )
        self.assertEqual(code, 200, data)
        rows = self.store.list_email_accounts()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], first["id"])
        self.assertEqual(rows[0]["email_address"], "b@163.com")
        self.assertEqual(rows[0]["password"], "p1")
        self.assertEqual(rows[0]["label"], "renamed")

    def test_disabled_not_returned_by_lookup(self):
        data, code = _handle_email_account_upsert(
            self.store,
            {
                "email_address": "off@163.com",
                "imap_host": "imap.163.com",
                "username": "off@163.com",
                "password": "p1",
                "enabled": 0,
            },
        )
        self.assertEqual(code, 200, data)
        self.assertIsNone(self.store.get_email_account_by_address("off@163.com"))

    def test_handle_admin_api_post_uses_body(self):
        result = handle_admin_api(
            method="POST",
            path="/admin/api/email-accounts",
            store=self.store,
            body={
                "email_address": "api@163.com",
                "imap_host": "imap.163.com",
                "username": "api@163.com",
                "password": "p1",
            },
        )
        self.assertIsNotNone(result)
        data, code = result
        self.assertEqual(code, 200, data)
        self.assertTrue(data.get("ok"))

    def test_invalid_port(self):
        data, code = _handle_email_account_upsert(
            self.store,
            {
                "email_address": "x@163.com",
                "imap_host": "imap.163.com",
                "imap_port": "nope",
                "username": "x@163.com",
                "password": "p1",
            },
        )
        self.assertEqual(code, 400)
        self.assertIn("imap_port", data.get("error", ""))


if __name__ == "__main__":
    unittest.main()
