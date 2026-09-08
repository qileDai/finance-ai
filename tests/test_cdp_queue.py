"""注册 FIFO、CDP 心跳锁、NNC1 让路、僵尸审核。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.browser.cdp_lock import (
    CdpHeartbeatLock,
    cdp_lock_held_here,
    pid_is_alive,
    set_cdp_lock_for_tests,
    should_skip_kill_cdp_chrome,
)
from src.browser.cdp_session import SessionWatchdog
from src.storage.db import ExternalGroupStore
from src.wework.icris_activation_worker import IcrisActivationWorker


class TestRegistrationFifoAndQueue(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ExternalGroupStore(
            db_path=Path(self._tmp.name) / "queue.db"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _enqueued_id(self, room: str) -> int:
        job, created = self.store.enqueue_registration_job(room, source="test")
        self.assertTrue(created)
        return int(job["id"])

    def test_claim_oldest_pending_first(self):
        first = self._enqueued_id("room-old")
        second = self._enqueued_id("room-new")
        self.assertLess(first, second)
        claimed = self.store.claim_next_job()
        self.assertIsNotNone(claimed)
        self.assertEqual(int(claimed["id"]), first)
        claimed2 = self.store.claim_next_job()
        self.assertEqual(int(claimed2["id"]), second)

    def test_backoff_pending_does_not_block_queue_flag(self):
        job, _ = self.store.enqueue_registration_job("room-bo", source="test")
        future = "9999-01-01T00:00:00+00:00"
        self.store.mark_job_failed(
            int(job["id"]),
            error="tmp",
            requeue=True,
            available_at=future,
        )
        self.assertFalse(self.store.has_active_registration_queue())
        self.assertIsNone(self.store.peek_claimable_registration())

    def test_due_pending_and_running_block_queue_flag(self):
        self._enqueued_id("room-p")
        self.assertTrue(self.store.has_active_registration_queue())
        self.store.claim_next_job()
        self.assertTrue(self.store.has_active_registration_queue())

    def test_fail_orphan_awaiting_review(self):
        job, _ = self.store.enqueue_registration_job("room-or", source="test")
        claimed = self.store.claim_next_job()
        self.store.mark_job_awaiting_review(int(claimed["id"]))
        self.assertTrue(self.store.has_active_registration_queue())
        n = self.store.fail_orphan_awaiting_review()
        self.assertEqual(n, 1)
        row = self.store.get_registration_job(int(job["id"]))
        self.assertEqual(row["status"], "failed")
        self.assertFalse(self.store.has_active_registration_queue())

    def test_form_failed_stores_screenshot(self):
        job, _ = self.store.enqueue_registration_job("room-fs", source="test")
        path = str(Path(self._tmp.name) / "form_fail.png")
        self.store.mark_job_form_failed(int(job["id"]), "boom", path)
        row = self.store.get_registration_job(int(job["id"]))
        self.assertEqual(row["form_status"], "failed")
        self.assertEqual(row["form_screenshot_path"], path)


class TestCdpHeartbeatLock(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.lock = CdpHeartbeatLock(
            mutex_path=root / "m.lock",
            lease_path=root / "lease.json",
            heartbeat_interval_s=0.15,
            stale_after_s=0.6,
        )
        set_cdp_lock_for_tests(self.lock)

    def tearDown(self) -> None:
        set_cdp_lock_for_tests(None)
        self._tmp.cleanup()

    def test_second_thread_waits_until_release(self):
        order: list[str] = []
        hold = self.lock.acquire("a")
        self.assertTrue(cdp_lock_held_here())

        def _other() -> None:
            h2 = self.lock.acquire("b")
            order.append("b")
            h2.release()

        t = threading.Thread(target=_other)
        t.start()
        time.sleep(0.25)
        self.assertEqual(order, [])
        hold.release()
        t.join(timeout=5)
        self.assertEqual(order, ["b"])

    def test_stale_lease_can_be_stolen(self):
        self.lock.lease_path.write_text(
            json.dumps({"owner": "dead", "pid": 1, "heartbeat": 1.0}),
            encoding="utf-8",
        )
        with patch.object(self.lock, "_kill_stale_chrome"):
            hold = self.lock.acquire("live")
        try:
            self.assertTrue(self.lock.held_in_this_thread())
            data = json.loads(self.lock.lease_path.read_text(encoding="utf-8"))
            self.assertEqual(data["owner"], "live")
        finally:
            hold.release()

    def test_skip_kill_when_other_holder_alive(self):
        hold = self.lock.acquire("a")
        try:
            skipped = {"v": None}

            def _other() -> None:
                skipped["v"] = should_skip_kill_cdp_chrome()

            t = threading.Thread(target=_other)
            t.start()
            t.join(timeout=2)
            self.assertTrue(skipped["v"])
        finally:
            hold.release()

    def test_self_pid_is_alive(self):
        self.assertTrue(pid_is_alive(os.getpid()))
        self.assertFalse(pid_is_alive(0))
        self.assertFalse(pid_is_alive(-1))
        self.assertFalse(pid_is_alive(999999))

    def test_dead_pid_fresh_heartbeat_not_alive(self):
        now = time.time()
        self.lock.lease_path.write_text(
            json.dumps({"owner": "ghost", "pid": 999999, "heartbeat": now}),
            encoding="utf-8",
        )
        self.assertFalse(self.lock.lease_is_alive())

    def test_acquire_steals_dead_pid_immediately(self):
        now = time.time()
        self.lock.lease_path.write_text(
            json.dumps({"owner": "ghost", "pid": 999999, "heartbeat": now}),
            encoding="utf-8",
        )
        t0 = time.time()
        with patch.object(self.lock, "_kill_stale_chrome"):
            hold = self.lock.acquire("live")
        try:
            self.assertLess(time.time() - t0, 0.5)
            self.assertTrue(self.lock.held_in_this_thread())
            data = json.loads(self.lock.lease_path.read_text(encoding="utf-8"))
            self.assertEqual(data["owner"], "live")
        finally:
            hold.release()

    def test_live_pid_fresh_heartbeat_not_stolen(self):
        now = time.time()
        self.lock.lease_path.write_text(
            json.dumps(
                {"owner": "other", "pid": os.getpid(), "heartbeat": now}
            ),
            encoding="utf-8",
        )
        self.assertTrue(self.lock.lease_is_alive())
        got: list[str] = []

        def _try() -> None:
            h = self.lock.acquire("waiter", poll_s=0.1)
            got.append("got")
            h.release()

        t = threading.Thread(target=_try)
        t.start()
        time.sleep(0.3)
        self.assertEqual(got, [])
        self.lock.lease_path.write_text("{}", encoding="utf-8")
        t.join(timeout=5)
        self.assertEqual(got, ["got"])


class TestActivationDoesNotFillAndNnc1Yields(unittest.TestCase):
    def test_check_pending_jobs_does_not_run_forms(self):
        store = MagicMock()
        store.get_jobs_pending_activation.return_value = [{"id": 1}]
        worker = IcrisActivationWorker(store)
        with patch.object(worker, "_process_one_job"), patch.object(
            worker, "_check_form_pending_jobs"
        ) as form_fn:
            worker._check_pending_jobs()
        form_fn.assert_not_called()

    def test_form_loop_skips_when_registration_queued(self):
        store = MagicMock()
        store.get_jobs_pending_form.return_value = [{"id": 3}]
        store.has_active_registration_queue.return_value = True
        worker = IcrisActivationWorker(store)
        with patch.object(worker, "_process_form_job") as proc:
            worker._check_form_pending_jobs()
        proc.assert_not_called()

    def test_form_job_releases_lock_without_browser_on_toctou(self):
        store = MagicMock()
        store.has_active_registration_queue.return_value = True
        store.get_email_account_by_address.return_value = None
        worker = IcrisActivationWorker(store)
        job = {
            "id": 4,
            "payload_json": json.dumps(
                {
                    "icris_account": {
                        "username": "u1",
                        "password": "p1",
                    }
                }
            ),
        }
        with patch("src.browser.cdp_session.hold_cdp_lock") as hold_fn, patch(
            "src.browser.icris_nnc1_form.IcrisNnc1FormBot"
        ) as bot_cls, patch(
            "src.browser.launcher.shutdown_cdp_chrome"
        ):
            hold_fn.return_value.__enter__ = MagicMock(return_value=None)
            hold_fn.return_value.__exit__ = MagicMock(return_value=False)
            worker._process_form_job(job)
        bot_cls.assert_called()
        bot_cls.return_value.run.assert_not_called()
        store.mark_job_form_failed.assert_not_called()
        store.mark_job_form_filled.assert_not_called()


class TestWatchdogPauseOnReview(unittest.TestCase):
    def test_paused_during_awaiting_review(self):
        store = MagicMock()
        store.get_job_status.return_value = "awaiting_review"
        with patch(
            "src.browser.cdp_session.capture_cdp_watchdog_screenshot"
        ) as shot, patch("src.browser.launcher.shutdown_cdp_chrome") as kill:
            wd = SessionWatchdog(0.2, job_id=9, store=store)
            wd.start()
            time.sleep(0.45)
            wd.stop()
        shot.assert_not_called()
        kill.assert_not_called()
        self.assertFalse(wd.fired)

    def test_fires_when_running(self):
        store = MagicMock()
        store.get_job_status.return_value = "running"
        with patch(
            "src.browser.cdp_session.capture_cdp_watchdog_screenshot"
        ) as shot, patch("src.browser.launcher.shutdown_cdp_chrome") as kill:
            wd = SessionWatchdog(0.15, job_id=9, store=store)
            wd.start()
            time.sleep(0.4)
            wd.stop()
        shot.assert_called()
        kill.assert_called()
        self.assertTrue(wd.fired)

    def test_watchdog_screenshots_before_kill(self):
        store = MagicMock()
        store.get_job_status.return_value = "running"
        order: list[str] = []

        def _shot(*_a, **_k):
            order.append("shot")
            return "watchdog.png"

        def _kill():
            order.append("kill")

        with patch(
            "src.browser.cdp_session.capture_cdp_watchdog_screenshot",
            side_effect=_shot,
        ), patch(
            "src.browser.launcher.shutdown_cdp_chrome",
            side_effect=_kill,
        ):
            wd = SessionWatchdog(0.15, job_id=9, store=store)
            wd.start()
            time.sleep(0.4)
            wd.stop()
        self.assertEqual(order, ["shot", "kill"])
        self.assertTrue(wd.fired)


class TestJobFormRetryUiContract(unittest.TestCase):
    def test_list_and_detail_share_failed_form_status(self):
        root = Path(__file__).resolve().parents[1]
        ui = (root / "web" / "admin" / "src" / "components" / "ui.tsx").read_text(
            encoding="utf-8"
        )
        jobs = (root / "web" / "admin" / "src" / "pages" / "JobsPage.tsx").read_text(
            encoding="utf-8"
        )
        detail = (
            root / "web" / "admin" / "src" / "pages" / "JobDetailPage.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn("export function jobCanFormRetry", ui)
        self.assertIn('=== "failed"', ui)
        self.assertIn("jobCanFormRetry(r.form_status)", jobs)
        self.assertIn("formRetryJob", jobs)
        self.assertIn("jobCanFormRetry(job.form_status)", detail)
        self.assertIn("确认重跑填表任务", jobs)
        self.assertIn("确认重跑填表任务", detail)
        self.assertIn("nnc1_duration", jobs)
        self.assertIn("nnc1填表", jobs)
        self.assertIn("job-error-cell", jobs)
        self.assertNotIn(".slice(0, 60)", jobs)
        self.assertIn("nnc1_duration", detail)


if __name__ == "__main__":
    unittest.main()
