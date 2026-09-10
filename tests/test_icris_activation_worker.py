import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.storage.db import ExternalGroupStore
from src.wework.icris_activation_worker import (
    IcrisActivationWorker,
    contact_email_from_payload,
    icris_credentials_from_payload,
)

S06_A = (
    "https://www.e-services.cr.gov.hk/ICRIS3EF/system/"
    "registration/s06.do?code=aaa"
)
S06_B = (
    "https://www.e-services.cr.gov.hk/ICRIS3EF/system/"
    "registration/s06.do?code=bbb"
)


def _payload(user: str, password: str = "Secret!", email: str = "a@x.com") -> str:
    return json.dumps(
        {
            "contact": {"email": email},
            "icris_account": {"username": user, "password": password},
        }
    )


def _pending_row(job_id: int, user: str, **extra) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "id": job_id,
        "status": "succeeded",
        "activation_status": "pending",
        "form_status": "",
        "activation_url": "",
        "activation_username": "",
        "activation_pending_at": now,
        "created_at": now,
        "payload_json": _payload(user),
        "activation_attempts": 0,
    }
    row.update(extra)
    return row


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
        job = {
            "id": 7,
            "payload_json": json.dumps({"contact": {"email": "a@x.com"}}),
        }
        store.get_registration_job.return_value = _pending_row(
            7, "", payload_json=job["payload_json"]
        )
        worker = IcrisActivationWorker(store)
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
        job = _pending_row(8, "MAWADA123")
        store.get_registration_job.return_value = job
        worker = IcrisActivationWorker(store)
        with patch("src.email.imap_client.EmailClient") as client_cls:
            client = client_cls.return_value
            client.fetch_activation_link.return_value = None
            worker._process_one_job(job)

        store.get_email_account_by_address.assert_called_once_with("a@x.com")
        args, kwargs = client.fetch_activation_link.call_args
        self.assertEqual(kwargs.get("expected_username"), "MAWADA123")
        store.mark_job_activated.assert_not_called()
        store.save_job_activation_url.assert_not_called()

    def test_persist_link_does_not_open_browser(self):
        store = MagicMock()
        store.get_email_account_by_address.return_value = {
            "imap_host": "imap.example.com",
            "imap_port": 993,
            "username": "a@x.com",
            "password": "auth",
        }
        store.save_job_activation_url.return_value = True
        job = _pending_row(9, "MAWADA123")
        store.get_registration_job.return_value = job
        worker = IcrisActivationWorker(store)
        mock_activate = AsyncMock(return_value=(True, "/tmp/shot.png"))
        with patch("src.email.imap_client.EmailClient") as client_cls, patch(
            "src.browser.icris_activation.activate_icris_account",
            mock_activate,
        ):
            client_cls.return_value.fetch_activation_link.return_value = S06_A
            worker._process_one_job(job)

        mock_activate.assert_not_called()
        store.save_job_activation_url.assert_called_once()
        args = store.save_job_activation_url.call_args[0]
        self.assertEqual(args[0], 9)
        self.assertIn("s06.do?code=aaa", args[1])
        self.assertEqual(args[2], "MAWADA123")
        store.mark_job_activated.assert_not_called()

    def test_skips_activate_when_registration_queue_busy(self):
        store = MagicMock()
        store.has_active_registration_queue.return_value = True
        store.get_jobs_pending_form.return_value = []
        store.get_jobs_ready_to_activate.return_value = [
            _pending_row(10, "MAWADA123", activation_url=S06_A, activation_username="MAWADA123")
        ]
        worker = IcrisActivationWorker(store)
        mock_activate = AsyncMock(return_value=(True, "/tmp/shot.png"))
        with patch(
            "src.browser.icris_activation.activate_icris_account",
            mock_activate,
        ):
            worker.drain()
        mock_activate.assert_not_called()
        store.claim_job_activation.assert_not_called()
        store.mark_job_activated.assert_not_called()

    def test_home_do_marks_failed_without_opening(self):
        store = MagicMock()
        store.get_email_account_by_address.return_value = {
            "imap_host": "imap.example.com",
            "imap_port": 993,
            "username": "a@x.com",
            "password": "auth",
        }
        job = _pending_row(11, "MAWADA123")
        store.get_registration_job.return_value = job
        worker = IcrisActivationWorker(store)
        mock_activate = AsyncMock(return_value=(True, "/tmp/shot.png"))
        home = "https://www.e-services.cr.gov.hk/ICRIS3EF/system/home.do"
        with patch("src.email.imap_client.EmailClient") as client_cls, patch(
            "src.browser.icris_activation.activate_icris_account",
            mock_activate,
        ):
            client_cls.return_value.fetch_activation_link.return_value = home
            worker._process_one_job(job)
        mock_activate.assert_not_called()
        store.mark_job_activated.assert_not_called()
        store.save_job_activation_url.assert_not_called()
        store.mark_job_activation_failed.assert_called_once()
        err = store.mark_job_activation_failed.call_args[0][1]
        self.assertIn("不是启动帐户链接", err)

    def test_already_activated_skips_imap(self):
        store = MagicMock()
        job = _pending_row(12, "MAWADA123", activation_status="activated")
        store.get_registration_job.return_value = job
        worker = IcrisActivationWorker(store)
        worker._process_one_job(job)
        store.get_email_account_by_address.assert_not_called()
        store.save_job_activation_url.assert_not_called()


class TestActivationDrainAndFifo(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ExternalGroupStore(
            db_path=Path(self._tmp.name) / "act.db"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _succeeded_job(self, room: str, user: str) -> int:
        job, created = self.store.enqueue_registration_job(
            room,
            source="test",
            payload={
                "contact": {"email": "a@x.com"},
                "icris_account": {"username": user, "password": "Secret!"},
            },
        )
        self.assertTrue(created)
        job_id = int(job["id"])
        claimed = self.store.claim_next_job()
        self.assertEqual(int(claimed["id"]), job_id)
        self.store.mark_job_succeeded(job_id)
        self.store.mark_job_activation_pending(job_id)
        return job_id

    def test_fifo_by_url_saved_at_not_job_id(self):
        later_id = self._succeeded_job("room-later-reg", "USERB")
        earlier_id = self._succeeded_job("room-earlier-reg", "USERA")
        self.assertGreater(earlier_id, later_id)
        self.assertTrue(self.store.save_job_activation_url(later_id, S06_B, "USERB"))
        self.assertTrue(self.store.save_job_activation_url(earlier_id, S06_A, "USERA"))
        ready = self.store.get_jobs_ready_to_activate()
        self.assertEqual([int(r["id"]) for r in ready], [later_id, earlier_id])

    def test_save_url_does_not_cross_accounts(self):
        a_id = self._succeeded_job("room-a", "USERA")
        b_id = self._succeeded_job("room-b", "USERB")
        self.assertTrue(self.store.save_job_activation_url(a_id, S06_A, "USERA"))
        self.assertTrue(self.store.save_job_activation_url(b_id, S06_B, "USERB"))
        row_a = self.store.get_registration_job(a_id)
        row_b = self.store.get_registration_job(b_id)
        self.assertEqual(row_a["activation_url"], S06_A)
        self.assertEqual(row_a["activation_username"], "USERA")
        self.assertEqual(row_b["activation_url"], S06_B)
        self.assertEqual(row_b["activation_username"], "USERB")

    def test_does_not_overwrite_existing_url(self):
        job_id = self._succeeded_job("room-ow", "USERA")
        self.assertTrue(self.store.save_job_activation_url(job_id, S06_A, "USERA"))
        self.assertFalse(self.store.save_job_activation_url(job_id, S06_B, "USERA"))
        row = self.store.get_registration_job(job_id)
        self.assertEqual(row["activation_url"], S06_A)

    def test_activated_not_listed_for_imap_or_browser(self):
        job_id = self._succeeded_job("room-done", "USERA")
        self.store.save_job_activation_url(job_id, S06_A, "USERA")
        claimed = self.store.claim_job_activation(job_id)
        self.assertIsNotNone(claimed)
        self.store.mark_job_activated(job_id)
        self.assertEqual(self.store.get_jobs_pending_activation(), [])
        self.assertEqual(self.store.get_jobs_ready_to_activate(), [])
        self.assertIsNone(self.store.claim_job_activation(job_id))

    def test_mark_pending_does_not_reset_activated(self):
        job_id = self._succeeded_job("room-keep", "USERA")
        self.store.save_job_activation_url(job_id, S06_A, "USERA")
        self.store.claim_job_activation(job_id)
        self.store.mark_job_activated(job_id)
        self.store.mark_job_activation_pending(job_id)
        row = self.store.get_registration_job(job_id)
        self.assertEqual(row["activation_status"], "activated")

    def test_cancelled_not_in_pending_activation(self):
        job, _ = self.store.enqueue_registration_job("room-cx", source="test")
        job_id = int(job["id"])
        self.store.mark_job_activation_pending(job_id)
        self.assertEqual(self.store.get_jobs_pending_activation(), [])

    def test_claim_prevents_double_open(self):
        job_id = self._succeeded_job("room-once", "USERA")
        self.store.save_job_activation_url(job_id, S06_A, "USERA")
        first = self.store.claim_job_activation(job_id)
        second = self.store.claim_job_activation(job_id)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_reset_stale_activating(self):
        job_id = self._succeeded_job("room-stale", "USERA")
        self.store.save_job_activation_url(job_id, S06_A, "USERA")
        self.store.claim_job_activation(job_id)
        n = self.store.reset_stale_activating_jobs()
        self.assertEqual(n, 1)
        row = self.store.get_registration_job(job_id)
        self.assertEqual(row["activation_status"], "pending")
        self.assertEqual(row["activation_url"], S06_A)

    def test_drain_activates_stored_link_with_matching_password(self):
        job_id = self._succeeded_job("room-drain", "MAWADA123")
        self.store.save_job_activation_url(job_id, S06_A, "MAWADA123")
        worker = IcrisActivationWorker(self.store)
        with patch.object(
            worker, "_run_activate_browser", return_value=(True, "/tmp/shot.png")
        ) as act, patch.object(worker, "_process_form_job") as form_fn:
            worker.drain()
        act.assert_called_once()
        args, kwargs = act.call_args
        self.assertEqual(args[0], S06_A)
        self.assertEqual(args[1], "MAWADA123")
        self.assertEqual(args[2], "Secret!")
        row = self.store.get_registration_job(job_id)
        self.assertEqual(row["activation_status"], "activated")
        form_fn.assert_called_once()

    def test_drain_skips_form_when_registration_arrives_after_activate(self):
        job_id = self._succeeded_job("room-gap", "USERA")
        self.store.save_job_activation_url(job_id, S06_A, "USERA")
        worker = IcrisActivationWorker(self.store)

        def _busy_after_activate(url, username, password):
            self.store.enqueue_registration_job("room-new-reg", source="test")
            return (True, "/tmp/shot.png")

        with patch.object(
            worker, "_run_activate_browser", side_effect=_busy_after_activate
        ), patch.object(worker, "_process_form_job") as form_fn:
            worker.drain()
        row = self.store.get_registration_job(job_id)
        self.assertEqual(row["activation_status"], "activated")
        self.assertEqual(row["form_status"], "pending")
        form_fn.assert_not_called()

    def test_mismatch_username_clears_url_without_activating(self):
        job_id = self._succeeded_job("room-mis", "USERA")
        self.store.save_job_activation_url(job_id, S06_A, "USERA")
        with self.store._conn() as conn:
            conn.execute(
                "UPDATE registration_jobs SET activation_username='OTHER' WHERE id=?",
                (job_id,),
            )
        worker = IcrisActivationWorker(self.store)
        with patch.object(
            worker, "_run_activate_browser", return_value=(True, "/tmp/ok.png")
        ) as act:
            worker.drain()
        act.assert_not_called()
        row = self.store.get_registration_job(job_id)
        self.assertEqual(row["activation_url"], "")
        self.assertEqual(row["activation_status"], "pending")

    def test_filled_form_not_listed(self):
        job_id = self._succeeded_job("room-filled", "USERA")
        self.store.save_job_activation_url(job_id, S06_A, "USERA")
        self.store.claim_job_activation(job_id)
        self.store.mark_job_activated(job_id)
        self.store.mark_job_form_filled(job_id, "/tmp/x.png")
        self.assertEqual(self.store.get_jobs_pending_form(), [])


if __name__ == "__main__":
    unittest.main()
