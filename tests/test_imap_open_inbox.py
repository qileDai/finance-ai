import imaplib
import unittest

from src.email.imap_client import (
    login_and_select_inbox,
    open_imap_inbox,
    select_imap_inbox,
)


class FakeImap:
    def __init__(self, *, require_id: bool = True, folders=None, select_ok: bool = True):
        self.require_id = require_id
        self.select_ok = select_ok
        self.folders = folders if folders is not None else [
            b'(\\HasNoChildren) "/" "INBOX"'
        ]
        self.id_sent = False
        self.selected = None
        self.searched = False
        self.logged_out = False
        self.state = "NONAUTH"

    def login(self, username, password):
        self.username = username
        self.password = password
        self.state = "AUTH"
        return "OK", [b"Logged in"]

    def _simple_command(self, name, arg=None):
        if name == "ID":
            self.id_sent = True
        return "OK", [b""]

    def _untagged_response(self, name):
        return "OK", [b""]

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
    def test_login_sends_id_and_selects_without_search(self):
        fake = FakeImap(require_id=True)
        login_and_select_inbox(fake, "u@163.com", "auth-code")
        self.assertTrue(fake.id_sent)
        self.assertEqual(fake.state, "SELECTED")
        self.assertFalse(fake.searched)

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


if __name__ == "__main__":
    unittest.main()
