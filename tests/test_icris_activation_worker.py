import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.wework.icris_activation_worker import (
    IcrisActivationWorker,
    contact_email_from_payload,
    icris_credentials_from_payload,
)


class TestActivationPayloadFields(unittest.TestCase):
    def test_contact_email_from_contact_then_applicant(self):
        self.assertEqual(
            contact_email_from_payload({"contact": {"email": "A@Example.com"}}),
            "a@example.com",
        )
        self.assertEqual(
            contact_email_from_payload({"applicant": {"email": "B@X.com"}}),
            "b@x.com",
        )
        self.assertEqual(contact_email_from_payload({}), "")

    def test_icris_credentials(self):
        user, pwd = icris_credentials_from_payload(
            {"icris_account": {"username": "MAWADA123", "password": "Secret!"}}
        )
        self.assertEqual(user, "MAWADA123")
        self.assertEqual(pwd, "Secret!")
        self.assertEqual(icris_credentials_from_payload({}), ("", ""))


class TestActivationWorkerMatch(unittest.TestCase):
    def test_skips_when_no_icris_username(self):
        store = MagicMock()
        worker = IcrisActivationWorker(store)
        job = {
            "id": 7,
            "payload_json": json.dumps({"contact": {"email": "a@x.com"}}),
        }
        worker._process_one_job(job)
        store.get_email_account_by_address.assert_not_called()
        store.mark_job_activation_failed.assert_not_called()
        store.mark_job_activated.assert_not_called()

    def test_fetch_uses_job_email_and_username(self):
        store = MagicMock()
        store.get_email_account_by_address.return_value = {
            "imap_host": "imap.example.com",
            "imap_port": 993,
            "username": "a@x.com",
            "password": "auth",
        }
        worker = IcrisActivationWorker(store)
        job = {
            "id": 8,
            "payload_json": json.dumps(
                {
                    "contact": {"email": "A@X.com"},
                    "icris_account": {
                        "username": "MAWADA123",
                        "password": "Secret!",
                    },
                }
            ),
        }
        with patch("src.email.imap_client.EmailClient") as client_cls:
            client = client_cls.return_value
            client.fetch_activation_link.return_value = None
            worker._process_one_job(job)

        store.get_email_account_by_address.assert_called_once_with("a@x.com")
        args, kwargs = client.fetch_activation_link.call_args
        self.assertEqual(kwargs.get("expected_username"), "MAWADA123")
        store.mark_job_activated.assert_not_called()

    def test_activate_passes_stored_password_then_marks_activated(self):
        store = MagicMock()
        store.get_email_account_by_address.return_value = {
            "imap_host": "imap.example.com",
            "imap_port": 993,
            "username": "a@x.com",
            "password": "auth",
        }
        worker = IcrisActivationWorker(store)
        job = {
            "id": 9,
            "payload_json": json.dumps(
                {
                    "contact": {"email": "a@x.com"},
                    "icris_account": {
                        "username": "MAWADA123",
                        "password": "Secret!",
                    },
                }
            ),
        }
        mock_activate = AsyncMock(return_value=(True, "/tmp/shot.png"))
        link = (
            "https://www.e-services.cr.gov.hk/ICRIS3EF/system/"
            "registration/s06.do?code=x"
        )
        with patch("src.email.imap_client.EmailClient") as client_cls, patch(
            "src.browser.icris_activation.activate_icris_account",
            mock_activate,
        ):
            client_cls.return_value.fetch_activation_link.return_value = link
            worker._process_one_job(job)

        mock_activate.assert_called_once()
        args, kwargs = mock_activate.call_args
        self.assertEqual(args[0], link)
        self.assertEqual(kwargs.get("username"), "MAWADA123")
        self.assertEqual(kwargs.get("password"), "Secret!")
        store.mark_job_activated.assert_called_once_with(9)
        store.mark_job_activation_failed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
