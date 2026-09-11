"""离线任务调度 E2E：入队 → Worker 认领 → 激活 → NNC1，不打 ICRIS/IMAP/Chrome。"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.browser.cdp_lock import CdpHeartbeatLock, set_cdp_lock_for_tests
from src.browser.icris_errors import IcrisFlowError, IcrisStepLoadError
from src.storage.db import ExternalGroupStore
from src.web.admin_api import job_pipeline_progress
from src.wework.icris_activation_worker import IcrisActivationWorker
from src.wework.icris_job_worker import IcrisJobWorker
from src.workflow.steps import WorkflowContext

S06 = (
    "https://www.e-services.cr.gov.hk/ICRIS3EF/system/"
    "registration/s06.do?code=sched"
)
EMAIL = "sched@example.com"


def _payload(user: str, password: str = "Secret!", email: str = EMAIL) -> dict:
    return {
        "contact": {"email": email},
        "icris_account": {"username": user, "password": password},
        "company_name_en": f"{user} LTD",
    }


class TestJobSchedulerE2E(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = ExternalGroupStore(db_path=root / "sched.db")
        self.lock = CdpHeartbeatLock(
            mutex_path=root / "m.lock",
            lease_path=root / "lease.json",
            heartbeat_interval_s=0.2,
            stale_after_s=0.8,
        )
        set_cdp_lock_for_tests(self.lock)
        self.store.upsert_email_account(
            EMAIL, "imap.example.com", 993, EMAIL, "auth"
        )
        self.workflow = MagicMock()
        self.workflow.run_icris_job.return_value = self._ok_ctx()
        self.job_worker = IcrisJobWorker(store=self.store, workflow=self.workflow)
        self.act = IcrisActivationWorker(self.store)
        self.act._notify_form_result = MagicMock()
        self.job_worker._activation_worker = self.act

        self._stack = ExitStack()
        self._stack.enter_context(patch("src.browser.launcher.shutdown_cdp_chrome"))
        self._stack.enter_context(patch("src.browser.cdp_session.SessionWatchdog"))
        imap_cls = self._stack.enter_context(
            patch("src.email.imap_client.EmailClient")
        )
        imap_cls.return_value.fetch_activation_link.return_value = S06
        self._bot_cls = self._stack.enter_context(
            patch("src.browser.icris_nnc1_form.IcrisNnc1FormBot")
        )
        self._bot = self._bot_cls.return_value
        self._bot.run = AsyncMock(return_value=(True, "ok"))
        self._bot._prelim_notified = False
        self._activate = self._stack.enter_context(
            patch.object(
                self.act,
                "_run_activate_browser",
                return_value=(True, "/tmp/shot.png"),
            )
        )

    def tearDown(self) -> None:
        self._stack.close()
        set_cdp_lock_for_tests(None)
        self._tmp.cleanup()

    def _ok_ctx(self) -> WorkflowContext:
        ctx = WorkflowContext()
        ctx.package_dir = Path(self._tmp.name) / "pkg"
        ctx.messages = ["ok"]
        return ctx

    def _job(self, job_id: int) -> dict:
        row = self.store.get_registration_job(job_id)
        self.assertIsNotNone(row)
        return row

    def _enqueue(self, room: str, user: str, source: str = "test") -> int:
        job, created = self.store.enqueue_registration_job(
            room,
            source=source,
            payload=_payload(user),
            company_name=f"{user} LTD",
        )
        self.assertTrue(created)
        return int(job["id"])

    def _seed_ready_to_activate(self, room: str, user: str) -> int:
        job_id = self._enqueue(room, user)
        claimed = self.store.claim_next_job()
        self.assertEqual(int(claimed["id"]), job_id)
        self.store.mark_job_succeeded(job_id)
        self.store.mark_job_activation_pending(job_id)
        self.assertTrue(self.store.save_job_activation_url(job_id, S06, user))
        return job_id

    def _seed_form_pending(self, room: str, user: str) -> int:
        job_id = self._seed_ready_to_activate(room, user)
        claimed = self.store.claim_job_activation(job_id)
        self.assertIsNotNone(claimed)
        self.store.mark_job_activated(job_id)
        return job_id

    def _reset_register_ok(self) -> None:
        self.workflow.run_icris_job.side_effect = None
        self.workflow.run_icris_job.return_value = self._ok_ctx()

    def test_two_jobs_full_pipeline_fifo_mixed_source(self):
        id_a = self._enqueue("room-a", "USERA", source="wework")
        id_b = self._enqueue("room-b", "USERB", source="admin")
        self.assertLess(id_a, id_b)

        self.job_worker._claim_and_run()
        self.assertEqual(self._job(id_a)["status"], "succeeded")
        self.assertEqual(self._job(id_b)["status"], "pending")
        self._activate.assert_not_called()

        self.act._process_one_job(self._job(id_a))
        self.job_worker._claim_and_run()
        self.assertEqual(self._job(id_a)["form_status"], "filled")
        self.assertEqual(self._job(id_b)["status"], "succeeded")
        self.assertNotEqual(self._job(id_b)["form_status"], "filled")

        self.act._process_one_job(self._job(id_b))
        self.act.drain()
        self.assertEqual(self._job(id_b)["form_status"], "filled")
        for jid in (id_a, id_b):
            prog = job_pipeline_progress(self._job(jid))
            self.assertEqual(prog["step"], "form_filled")
            self.assertEqual(prog["label"], "已填表")

    def test_claim_and_run_does_not_drain_while_registration_queued(self):
        ready = self._seed_ready_to_activate("room-ready", "READY")
        self._enqueue("room-a", "USERA")
        self._enqueue("room-b", "USERB")

        self.job_worker._claim_and_run()
        self._activate.assert_not_called()
        self._bot.run.assert_not_called()
        row = self._job(ready)
        self.assertEqual(row["activation_status"], "pending")
        self.assertTrue(row["activation_url"])
        self.assertIsNotNone(self.store.peek_claimable_registration())

    def test_idle_queue_drains_to_form_filled(self):
        job_id = self._enqueue("room-idle", "IDLE")
        self.job_worker._claim_and_run()
        row = self._job(job_id)
        self.assertEqual(row["status"], "succeeded")
        self.assertEqual(row["activation_status"], "pending")
        self._activate.assert_not_called()

        self.act._process_one_job(self._job(job_id))
        self.job_worker._claim_and_run()
        row = self._job(job_id)
        self.assertEqual(row["activation_status"], "activated")
        self.assertEqual(row["form_status"], "filled")
        self.assertEqual(job_pipeline_progress(row)["label"], "已填表")

    def test_nnc1_yields_when_registration_enqueued(self):
        form_id = self._seed_form_pending("room-form", "FORM1")
        self._enqueue("room-new-reg", "NEWREG")
        self.act.drain()
        self._bot.run.assert_not_called()
        self.assertEqual(self._job(form_id)["form_status"], "pending")

    def test_backoff_does_not_block_nnc1_but_review_does(self):
        form_id = self._seed_form_pending("room-form", "FORM1")
        other = self._enqueue("room-bo", "BACK")
        self.store.mark_job_failed(
            other,
            error="tmp",
            requeue=True,
            available_at="9999-01-01T00:00:00+00:00",
        )
        self.assertFalse(self.store.has_active_registration_queue())
        self.act.drain()
        self.assertEqual(self._job(form_id)["form_status"], "filled")

        form2 = self._seed_form_pending("room-form2", "FORM2")
        rev = self._enqueue("room-rev", "REV")
        claimed = self.store.claim_next_job()
        self.assertEqual(int(claimed["id"]), rev)
        self.store.mark_job_awaiting_review(rev)
        self._bot.run.reset_mock()
        self.act.drain()
        self.assertEqual(self._job(form2)["form_status"], "pending")
        self._bot.run.assert_not_called()

        approved = self.store.approve_job_submit(rev)
        self.assertEqual(approved["review_status"], "approved")
        self.assertEqual(approved["status"], "awaiting_review")
        self.assertTrue(self.store.has_active_registration_queue())

    def test_register_retry_vs_no_requeue(self):
        retry_id = self._enqueue("room-retry", "RETRY")
        self.workflow.run_icris_job.side_effect = IcrisStepLoadError(
            "未进入身份证明页: https://example"
        )
        self.job_worker._claim_and_run()
        row = self._job(retry_id)
        self.assertEqual(row["status"], "pending")
        self.assertTrue(str(row.get("available_at") or "").strip())
        self.assertNotEqual(str(row.get("activation_status") or ""), "pending")

        self._reset_register_ok()
        self.workflow.run_icris_job.side_effect = IcrisFlowError(
            "s04 香港身分證號碼格式不正確！",
            no_requeue=True,
        )
        fail_id = self._enqueue("room-noreq", "NOREQ")
        self.job_worker._claim_and_run()
        row2 = self._job(fail_id)
        self.assertEqual(row2["status"], "failed")
        self.assertNotEqual(str(row2.get("activation_status") or ""), "pending")

    def test_cancel_pending_and_running(self):
        pending_id = self._enqueue("room-cx-p", "CXP")
        self.store.cancel_registration_job(pending_id)
        self.assertEqual(self._job(pending_id)["status"], "cancelled")
        self.assertIsNone(self.store.peek_claimable_registration())

        early_id = self._enqueue("room-cx-early", "CXE")
        claimed = self.store.claim_next_job()
        self.store.cancel_registration_job(early_id)
        self.workflow.run_icris_job.reset_mock()
        self.job_worker._process_job(claimed)
        self.workflow.run_icris_job.assert_not_called()
        self.assertEqual(self._job(early_id)["status"], "cancelled")

        mid_id = self._enqueue("room-cx-mid", "CXM")

        def _cancel_and_raise(job, **_kwargs):
            self.store.cancel_registration_job(int(job["id"]))
            raise RuntimeError("browser died")

        self.workflow.run_icris_job.side_effect = _cancel_and_raise
        self.job_worker._claim_and_run()
        self.assertEqual(self._job(mid_id)["status"], "cancelled")

    def test_admin_requeue_and_form_retry(self):
        self.workflow.run_icris_job.side_effect = IcrisFlowError(
            "boom", no_requeue=True
        )
        job_id = self._enqueue("room-rq", "RQ")
        self.job_worker._claim_and_run()
        self.assertEqual(self._job(job_id)["status"], "failed")

        self._reset_register_ok()
        rq = self.store.requeue_registration_job(job_id)
        self.assertEqual(rq["status"], "pending")
        self.job_worker._claim_and_run()
        self.assertEqual(self._job(job_id)["status"], "succeeded")

        form_id = self._seed_form_pending("room-fr", "FR")
        still = self.store.reset_job_form_retry(form_id)
        self.assertEqual(still["form_status"], "pending")
        self._bot.run = AsyncMock(return_value=(False, "nnc1 boom"))
        self.act.drain()
        self.assertEqual(self._job(form_id)["form_status"], "failed")

        retried = self.store.reset_job_form_retry(form_id)
        self.assertEqual(retried["form_status"], "pending")
        self._bot.run = AsyncMock(return_value=(True, "ok"))
        self.act.drain()
        self.assertEqual(self._job(form_id)["form_status"], "filled")

        fail_id = self._enqueue("room-block", "BLK1")
        self.workflow.run_icris_job.side_effect = IcrisFlowError(
            "fail", no_requeue=True
        )
        self.job_worker._claim_and_run()
        self.assertEqual(self._job(fail_id)["status"], "failed")
        active_id = self._enqueue("room-block", "BLK2")
        blocked = self.store.requeue_registration_job(fail_id)
        self.assertEqual(blocked["status"], "failed")
        self.assertEqual(self._job(active_id)["status"], "pending")

    def test_restart_recovers_running_and_orphan_review(self):
        run_id = self._enqueue("room-stale", "STALE")
        claimed = self.store.claim_next_job()
        self.assertEqual(int(claimed["id"]), run_id)
        self.assertEqual(self._job(run_id)["status"], "running")
        n = self.store.reset_stale_running_jobs(older_than_minutes=0)
        self.assertEqual(n, 1)
        self.assertEqual(self._job(run_id)["status"], "pending")
        self._reset_register_ok()
        self.job_worker._claim_and_run()
        self.assertEqual(self._job(run_id)["status"], "succeeded")

        form_id = self._seed_form_pending("room-form-or", "FORPH")
        rev = self._enqueue("room-or", "ORPH")
        claimed_rev = self.store.claim_next_job()
        self.store.mark_job_awaiting_review(int(claimed_rev["id"]))
        self.assertTrue(self.store.has_active_registration_queue())
        self._bot.run.reset_mock()
        self.act.drain()
        self.assertEqual(self._job(form_id)["form_status"], "pending")
        self._bot.run.assert_not_called()

        orphans = self.store.fail_orphan_awaiting_review()
        self.assertEqual(orphans, 1)
        self.assertEqual(self._job(rev)["status"], "failed")
        self.assertFalse(self.store.has_active_registration_queue())
        self.act.drain()
        self.assertEqual(self._job(form_id)["form_status"], "filled")

    def test_activation_fail_kinds(self):
        busy_id = self._seed_ready_to_activate("room-busy", "BUSY")
        blocker = self._enqueue("room-reg", "REG")
        claimed = self.store.claim_job_activation(busy_id)
        self.assertIsNotNone(claimed)
        ok = self.act._activate_claimed_job(claimed)
        self.assertFalse(ok)
        self._activate.assert_not_called()
        row = self._job(busy_id)
        self.assertEqual(row["activation_status"], "pending")
        self.assertEqual(row["activation_url"], S06)
        self.store.cancel_registration_job(blocker)

        cred_id = self._seed_ready_to_activate("room-cred", "CRED")
        self._activate.return_value = (False, "incorrect user")
        self.act.drain()
        cred = self._job(cred_id)
        self.assertEqual(cred["activation_status"], "failed")
        self.assertNotEqual(cred["form_status"], "filled")
        self._bot.run.assert_not_called()

        transient_id = self._seed_ready_to_activate("room-tr", "TRANS")
        self._activate.return_value = (False, "network timeout")
        self._activate.reset_mock()
        self.act.drain()
        tr = self._job(transient_id)
        self.assertEqual(tr["activation_status"], "pending")
        self.assertEqual(tr["activation_url"], S06)

        self._activate.return_value = (True, "/tmp/shot.png")
        self.act.drain()
        self.assertEqual(self._job(transient_id)["form_status"], "filled")

        stale_id = self._seed_ready_to_activate("room-stale-url", "STALEU")
        self._activate.return_value = (False, "打开后不是启动帐户页")
        self.act.drain()
        stale = self._job(stale_id)
        self.assertEqual(stale["activation_status"], "pending")
        self.assertEqual(stale["activation_url"], "")

        job, created = self.store.enqueue_registration_job(
            "room-nomail",
            source="test",
            payload=_payload("NOMAIL", email="missing@example.com"),
        )
        self.assertTrue(created)
        miss_id = int(job["id"])
        claimed_m = self.store.claim_next_job()
        self.assertEqual(int(claimed_m["id"]), miss_id)
        self.store.mark_job_succeeded(miss_id)
        self.store.mark_job_activation_pending(miss_id)
        self.act._process_one_job(self._job(miss_id))
        miss = self._job(miss_id)
        self.assertEqual(miss["activation_status"], "failed")
        self.assertIn("IMAP", miss["last_error"])

    def test_duplicate_enqueue_and_rejected_not_claimed(self):
        first = self._enqueue("room-dup", "DUP")
        job, created = self.store.enqueue_registration_job(
            "room-dup",
            source="test",
            payload=_payload("DUP2"),
        )
        self.assertFalse(created)
        self.assertEqual(int(job["id"]), first)
        self.store.cancel_registration_job(first)

        rej = self._enqueue("room-rej", "REJ")
        claimed = self.store.claim_next_job()
        self.assertEqual(int(claimed["id"]), rej)
        self.store.mark_job_awaiting_review(rej)
        rejected = self.store.reject_job_submit(rej)
        self.assertEqual(rejected["review_status"], "rejected")
        self.assertEqual(rejected["status"], "failed")
        self.assertIsNone(self.store.peek_claimable_registration())
        self.assertIsNone(self.store.claim_next_job())


if __name__ == "__main__":
    unittest.main()
