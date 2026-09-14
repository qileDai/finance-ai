"""步骤日志 TTL 清理：只清空过期已结束任务，不删行、不碰进行中流水线。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.storage.db import ExternalGroupStore
from src.wework.job_log_cleanup import JobLogCleanupWorker


class TestJobLogCleanup(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ExternalGroupStore(db_path=Path(self._tmp.name) / "jobs.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _enqueue(self, room: str) -> int:
        job, created = self.store.enqueue_registration_job(
            room, source="test", payload={"company_name_en": room}
        )
        self.assertTrue(created)
        return int(job["id"])

    def _succeed_with_logs(self, job_id: int, text: str = "step log") -> None:
        claimed = self.store.claim_next_job()
        self.assertEqual(int(claimed["id"]), job_id)
        self.store.mark_job_succeeded(
            job_id,
            result_messages=[{"level": "INFO", "message": text, "time": "08:00:00"}],
        )

    def _age(self, job_id: int, *, days: int) -> None:
        old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self.store._conn() as conn:
            conn.execute(
                "UPDATE registration_jobs SET updated_at=? WHERE id=?",
                (old, job_id),
            )

    def test_purge_old_finished_keeps_row(self) -> None:
        job_id = self._enqueue("done-old")
        self._succeed_with_logs(job_id, "keep-me-until-purge")
        self.store.mark_job_activation_pending(job_id)
        self.store.mark_job_activated(job_id)
        self.store.mark_job_form_filled(job_id, "")
        self._age(job_id, days=20)
        n = self.store.purge_old_job_result_messages(14)
        self.assertEqual(n, 1)
        row = self.store.get_registration_job(job_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "succeeded")
        self.assertEqual(self.store.get_job_result_messages(job_id), [])

    def test_skip_activation_pending_and_form_pending(self) -> None:
        act_id = self._enqueue("act-pending")
        self._succeed_with_logs(act_id, "activation still pending")
        self.store.mark_job_activation_pending(act_id)
        self._age(act_id, days=20)

        form_id = self._enqueue("form-pending")
        self._succeed_with_logs(form_id, "form still pending")
        self.store.mark_job_activation_pending(form_id)
        self.store.mark_job_activated(form_id)
        self._age(form_id, days=20)

        n = self.store.purge_old_job_result_messages(14)
        self.assertEqual(n, 0)
        self.assertEqual(
            self.store.get_job_result_messages(act_id)[0]["message"],
            "activation still pending",
        )
        self.assertEqual(
            self.store.get_job_result_messages(form_id)[0]["message"],
            "form still pending",
        )

    def test_skip_running_job(self) -> None:
        job_id = self._enqueue("running")
        claimed = self.store.claim_next_job()
        self.assertEqual(int(claimed["id"]), job_id)
        self.store.update_job_result_messages(
            job_id, [{"level": "INFO", "message": "still running"}]
        )
        self._age(job_id, days=20)
        n = self.store.purge_old_job_result_messages(14)
        self.assertEqual(n, 0)
        self.assertEqual(
            self.store.get_job_result_messages(job_id)[0]["message"],
            "still running",
        )

    def test_zero_days_disabled(self) -> None:
        job_id = self._enqueue("no-clean")
        self._succeed_with_logs(job_id)
        self._age(job_id, days=20)
        self.assertEqual(self.store.purge_old_job_result_messages(0), 0)
        self.assertTrue(self.store.get_job_result_messages(job_id))

    def test_cleanup_worker_run_once(self) -> None:
        job_id = self._enqueue("worker")
        self._succeed_with_logs(job_id, "old")
        self.store.mark_job_activation_pending(job_id)
        self.store.mark_job_activated(job_id)
        self.store.mark_job_form_filled(job_id, "")
        self._age(job_id, days=20)
        worker = JobLogCleanupWorker(
            self.store, retention_days=14, interval_seconds=86400
        )
        self.assertEqual(worker.run_once(), 1)
        self.assertEqual(self.store.get_job_result_messages(job_id), [])


if __name__ == "__main__":
    unittest.main()
