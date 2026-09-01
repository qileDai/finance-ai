import imaplib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.email.imap_client import (
    _send_imap_id,
    format_imap_connect_error,
    login_and_select_inbox,
    open_imap_inbox,
    select_imap_inbox,
)
from src.storage.db import ExternalGroupStore
from src.web.admin_api import _handle_email_account_test, _handle_email_account_upsert


class FakeImap:
    def __init__(self, *, require_id: bool = True, folders=None, select_ok: bool = True):
        self.require_id = require_id
        self.select_ok = select_ok
        self.folders = folders if folders is not None else [
            b'(\\HasNoChildren) "/" "INBOX"'
        ]
        self.id_sent = False
        self.id_states: list[str] = []
        self.id_payload = ""
        self.untagged_calls: list[tuple] = []
        self.selected = None
        self.searched = False
        self.logged_out = False
        self.state = "NONAUTH"

    def login(self, username, password):
        self.username = username
        self.password = password
        self.state = "AUTH"
        return "OK", [b"Logged in"]

    def _simple_command(self, name, *args):
        if name == "ID":
            self.id_sent = True
            self.id_states.append(self.state)
            self.id_payload = str(args[0]) if args else ""
        return "OK", [b""]

    def _untagged_response(self, typ, dat=None, name=None):
        self.untagged_calls.append((typ, dat, name))
        return typ, dat

    def select(self, mailbox):
        if not self.select_ok:
            self.state = "AUTH"
            return "NO", [b"Unsafe Login"]
        if self.require_id and not self.id_sent:
            self.state = "AUTH"
            return "NO", [b"Unsafe Login. Please use IMAP ID"]
        name = str(mailbox).strip('"')
        if name.upper() == "INBOX" or name == "收件箱":
            self.selected = mailbox
            self.state = "SELECTED"
            return "OK", [b"1"]
        return "NO", [b"Mailbox does not exist"]

    def list(self, *a, **k):
        return "OK", self.folders

    def search(self, *a, **k):
        self.searched = True
        if self.state != "SELECTED":
            raise imaplib.IMAP4.error(
                "command SEARCH illegal in state AUTH, only allowed in states SELECTED"
            )
        return "OK", [b"1 2"]

    def logout(self):
        self.logged_out = True
        self.state = "LOGOUT"
        return "BYE", []


class TestOpenImapInbox(unittest.TestCase):
    def test_login_sends_id_before_login(self):
        fake = FakeImap(require_id=True)
        login_and_select_inbox(fake, "u@163.com", "auth-code")
        self.assertTrue(fake.id_sent)
        self.assertGreaterEqual(len(fake.id_states), 1)
        self.assertEqual(fake.id_states[0], "NONAUTH")
        self.assertIn("contact", fake.id_payload)
        self.assertIn("u@163.com", fake.id_payload)
        self.assertTrue(any(c[2] == "ID" for c in fake.untagged_calls))
        self.assertEqual(fake.state, "SELECTED")
        self.assertFalse(fake.searched)

    def test_send_imap_id_uses_three_arg_untagged_response(self):
        fake = FakeImap()
        _send_imap_id(fake, "a@163.com")
        self.assertEqual(len(fake.untagged_calls), 1)
        typ, dat, name = fake.untagged_calls[0]
        self.assertEqual(typ, "OK")
        self.assertEqual(name, "ID")

    def test_open_imap_inbox_success_does_not_search(self):
        fake = FakeImap(require_id=True)
        mail = open_imap_inbox(
            "imap.163.com",
            993,
            "u@163.com",
            "auth-code",
            connect=lambda: fake,
        )
        self.assertIs(mail, fake)
        self.assertTrue(fake.id_sent)
        self.assertEqual(fake.state, "SELECTED")
        self.assertFalse(fake.searched)
        self.assertFalse(fake.logged_out)

    def test_select_failure_does_not_search(self):
        fake = FakeImap(select_ok=False)
        with self.assertRaises(RuntimeError) as ctx:
            login_and_select_inbox(fake, "u@163.com", "auth-code")
        self.assertIn("无法打开收件箱", str(ctx.exception))
        self.assertFalse(fake.searched)
        self.assertEqual(fake.state, "AUTH")

    def test_select_ok_but_still_auth_is_failure(self):
        fake = FakeImap(require_id=False)

        def select(mailbox):
            fake.selected = mailbox
            fake.state = "AUTH"
            return "ok", [b"1"]

        fake.select = select  # type: ignore[method-assign]
        with self.assertRaises(RuntimeError) as ctx:
            select_imap_inbox(fake)
        self.assertIn("无法打开收件箱", str(ctx.exception))
        self.assertIn("AUTH", str(ctx.exception))
        self.assertFalse(fake.searched)

    def test_list_fallback_selects_inbox_from_list(self):
        fake = FakeImap(require_id=False)
        attempts: list[str] = []

        def select(mailbox):
            attempts.append(str(mailbox))
            if mailbox == "Inbox":
                fake.selected = mailbox
                fake.state = "SELECTED"
                return "OK", [b"1"]
            fake.state = "AUTH"
            return "NO", [b"no"]

        fake.select = select  # type: ignore[method-assign]
        fake.folders = [b'(\\HasNoChildren) "/" "Inbox"']
        select_imap_inbox(fake)
        self.assertEqual(fake.selected, "Inbox")
        self.assertIn("Inbox", attempts)
        self.assertFalse(fake.searched)

    def test_open_failure_logs_out(self):
        fake = FakeImap(select_ok=False)
        with self.assertRaises(RuntimeError):
            open_imap_inbox(
                "imap.163.com",
                993,
                "u@163.com",
                "auth-code",
                connect=lambda: fake,
            )
        self.assertTrue(fake.logged_out)
        self.assertFalse(fake.searched)

    def test_format_search_illegal_auth(self):
        msg = format_imap_connect_error(
            imaplib.IMAP4.error(
                "command SEARCH illegal in state AUTH, only allowed in states SELECTED"
            )
        )
        self.assertIn("AUTH", msg)
        self.assertIn("IMAP ID", msg)

    def test_format_unsafe_login(self):
        msg = format_imap_connect_error(RuntimeError("Unsafe Login"))
        self.assertIn("Unsafe Login", msg)
        self.assertIn("授权码", msg)


class TestEmailAccountTestApi(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ExternalGroupStore(
            db_path=Path(self._tmp.name) / "test_email.db"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _save(self) -> int:
        data, code = _handle_email_account_upsert(
            self.store,
            {
                "email_address": "a@163.com",
                "imap_host": "imap.163.com",
                "imap_port": 993,
                "username": "a@163.com",
                "password": "auth-code",
            },
        )
        self.assertEqual(code, 200, data)
        return int(data["account"]["id"])

    def test_missing_password(self):
        aid = self._save()
        with self.store._conn() as conn:
            conn.execute(
                "UPDATE email_accounts SET password='' WHERE id=?", (aid,)
            )
        data, code = _handle_email_account_test(self.store, aid)
        self.assertEqual(code, 400)
        self.assertIn("授权码", data.get("error", ""))

    def test_success_requires_selected(self):
        aid = self._save()
        fake = FakeImap()
        fake.state = "SELECTED"
        with patch(
            "src.email.imap_client.open_imap_inbox",
            return_value=fake,
        ) as mocked_open:
            data, code = _handle_email_account_test(self.store, aid)
        mocked_open.assert_called_once()
        self.assertEqual(code, 200, data)
        self.assertIn("收件箱", data.get("message", ""))
        self.assertTrue(fake.logged_out)

    def test_auth_state_is_failure(self):
        aid = self._save()
        fake = FakeImap()
        fake.state = "AUTH"
        with patch(
            "src.email.imap_client.open_imap_inbox",
            return_value=fake,
        ):
            data, code = _handle_email_account_test(self.store, aid)
        self.assertEqual(code, 400)
        self.assertIn("收件箱", data.get("error", ""))


if __name__ == "__main__":
    unittest.main()
