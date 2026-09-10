"""注册任务全流程进度：job_pipeline_progress 与列表筛选。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.storage.db import ExternalGroupStore
from src.web.admin_api import job_pipeline_progress


class TestJobPipelineProgress(unittest.TestCase):
    def test_pending_is_queued(self):
        p = job_pipeline_progress({"status": "pending"})
        self.assertEqual(p["step"], "queued")
        self.assertEqual(p["label"], "排队")
        self.assertFalse(p["failed"])

    def test_succeeded_activation_pending(self):
        p = job_pipeline_progress(
            {
                "status": "succeeded",
                "activation_status": "pending",
                "form_status": "",
            }
        )
        self.assertEqual(p["step"], "activating")
        self.assertEqual(p["label"], "待激活")
        self.assertFalse(p["failed"])

    def test_succeeded_form_pending(self):
        p = job_pipeline_progress(
            {
                "status": "succeeded",
                "activation_status": "activated",
                "form_status": "pending",
            }
        )
        self.assertEqual(p["step"], "form_pending")
        self.assertEqual(p["label"], "待填表")

    def test_form_filled(self):
        p = job_pipeline_progress(
            {
                "status": "succeeded",
                "activation_status": "activated",
                "form_status": "filled",
            }
        )
        self.assertEqual(p["step"], "form_filled")
        self.assertEqual(p["label"], "已填表")
        self.assertFalse(p["failed"])

    def test_review_rejected(self):
        p = job_pipeline_progress(
            {
                "status": "failed",
                "review_status": "rejected",
                "last_error": "审核拒绝",
            }
        )
        self.assertEqual(p["step"], "review")
        self.assertEqual(p["label"], "已拒绝")
        self.assertTrue(p["failed"])
        self.assertIn("审核拒绝", p["detail"])


class TestJobProgressListFilter(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ExternalGroupStore(db_path=Path(self._tmp.name) / "jobs.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _enqueue(self, room: str) -> int:
        job, created = self.store.enqueue_registration_job(room, source="test")
        self.assertTrue(created)
        return int(job["id"])

    def _update(self, job_id: int, **fields: object) -> None:
        keys = list(fields.keys())
        vals = [fields[k] for k in keys]
        with self.store._conn() as conn:
            sets = ", ".join(f"{k}=?" for k in keys)
            conn.execute(
                f"UPDATE registration_jobs SET {sets} WHERE id=?",
                (*vals, job_id),
            )

    def test_form_pending_excludes_activation_pending(self):
        waiting_act = self._enqueue("act")
        waiting_form = self._enqueue("form")
        self._update(
            waiting_act,
            status="succeeded",
            activation_status="pending",
            form_status="",
        )
        self._update(
            waiting_form,
            status="succeeded",
            activation_status="activated",
            form_status="pending",
        )
        rows = self.store.list_registration_jobs(status="form_pending")
        ids = [int(r["id"]) for r in rows]
        self.assertEqual(ids, [waiting_form])
        self.assertEqual(self.store.count_registration_jobs(status="form_pending"), 1)

        act_rows = self.store.list_registration_jobs(status="activation_pending")
        self.assertEqual([int(r["id"]) for r in act_rows], [waiting_act])

    def test_legacy_pending_filter(self):
        a = self._enqueue("p1")
        b = self._enqueue("p2")
        self._update(b, status="running")
        rows = self.store.list_registration_jobs(status="pending")
        self.assertEqual([int(r["id"]) for r in rows], [a])
        queued = self.store.list_registration_jobs(status="queued")
        self.assertEqual([int(r["id"]) for r in queued], [a])


if __name__ == "__main__":
    unittest.main()
