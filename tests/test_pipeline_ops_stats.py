"""运营统计 pipeline_ops_stats：积压、成功率、耗时、按日序列。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.storage.db import ExternalGroupStore


class TestPipelineOpsStats(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ExternalGroupStore(db_path=Path(self._tmp.name) / "ops.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _enqueue(self, room: str, *, source: str = "wework") -> int:
        job, created = self.store.enqueue_registration_job(room, source=source)
        self.assertTrue(created)
        return int(job["id"])

    def _claim(self) -> int:
        claimed = self.store.claim_next_job()
        self.assertIsNotNone(claimed)
        return int(claimed["id"])

    def _update(self, job_id: int, **fields: object) -> None:
        keys = list(fields.keys())
        vals = [fields[k] for k in keys]
        with self.store._conn() as conn:
            sets = ", ".join(f"{k}=?" for k in keys)
            conn.execute(
                f"UPDATE registration_jobs SET {sets} WHERE id=?",
                (*vals, job_id),
            )

    def test_empty_stats(self) -> None:
        stats = self.store.pipeline_ops_stats(hours=24)
        self.assertEqual(stats["backlog"]["register_pending"], 0)
        self.assertEqual(stats["stages"]["register"]["success"], 0)
        self.assertEqual(stats["stages"]["register"]["success_rate"], 0.0)
        self.assertEqual(stats["extras"]["created"], 0)
        self.assertGreaterEqual(len(stats["daily"]), 1)

    def test_backlog_snapshot(self) -> None:
        ids = [self._enqueue(f"r{i}") for i in range(6)]
        self._update(ids[0], status="pending")
        self._update(ids[1], status="pending")
        self._update(ids[2], status="running")
        self._update(
            ids[3], status="awaiting_review", review_status="awaiting_review"
        )
        self._update(ids[4], status="succeeded", activation_status="pending")
        self._update(
            ids[5],
            status="succeeded",
            activation_status="activated",
            form_status="pending",
        )

        stats = self.store.pipeline_ops_stats(hours=24)
        b = stats["backlog"]
        self.assertEqual(b["register_pending"], 2)
        self.assertEqual(b["register_running"], 1)
        self.assertEqual(b["awaiting_review"], 1)
        self.assertEqual(b["activation_pending"], 1)
        self.assertEqual(b["form_pending"], 1)

    def test_stage_rates_and_durations(self) -> None:
        ok_id = self._enqueue("ok", source="admin")
        self._claim()
        self.store.mark_job_succeeded(ok_id, run_duration="4分0秒")
        self._update(ok_id, s03a_duration="1分0秒")
        self.store.mark_job_activation_pending(ok_id)
        past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        self._update(ok_id, activation_checked_at=past)
        self.store.mark_job_activated(ok_id)
        self._update(ok_id, activation_checked_at=past, activation_activated_at=now)
        self.store.mark_job_form_filled(ok_id, "/tmp/f.png", nnc1_duration="2分0秒")

        fail_id = self._enqueue("fail", source="wework")
        self._claim()
        self.store.mark_job_failed(fail_id, error="boom", run_duration="6分0秒")

        act_fail = self._enqueue("act-fail", source="admin")
        self._claim()
        self.store.mark_job_succeeded(act_fail, run_duration="1分0秒")
        self.store.mark_job_activation_failed(act_fail, "激活失败")

        form_fail = self._enqueue("form-fail", source="wework")
        self._claim()
        self.store.mark_job_succeeded(form_fail, run_duration="1分30秒")
        self.store.mark_job_activated(form_fail)
        self.store.mark_job_form_failed(form_fail, "填表失败", nnc1_duration="3分0秒")

        stats = self.store.pipeline_ops_stats(hours=24)
        reg = stats["stages"]["register"]
        self.assertEqual(reg["success"], 3)
        self.assertEqual(reg["failed"], 1)
        self.assertEqual(reg["success_rate"], 0.75)
        self.assertEqual(reg["fail_rate"], 0.25)
        self.assertGreater(reg["duration"]["avg_minutes"], 0)
        self.assertGreater(reg["s03a_duration"]["avg_minutes"], 0)

        act = stats["stages"]["activation"]
        self.assertEqual(act["success"], 2)
        self.assertEqual(act["failed"], 1)
        self.assertEqual(act["success_rate"], round(2 / 3, 4))

        form = stats["stages"]["form"]
        self.assertEqual(form["success"], 1)
        self.assertEqual(form["failed"], 1)
        self.assertEqual(form["success_rate"], 0.5)
        self.assertEqual(form["duration"]["avg_minutes"], 2.5)

        extras = stats["extras"]
        self.assertEqual(extras["created"], 4)
        self.assertEqual(extras["e2e_filled"], 1)
        self.assertEqual(extras["source"]["admin"], 2)
        self.assertEqual(extras["source"]["wework"], 2)

        today = datetime.now(timezone.utc).date().isoformat()
        today_row = next(d for d in stats["daily"] if d["date"] == today)
        self.assertGreaterEqual(today_row["created"], 4)
        self.assertGreaterEqual(today_row["succeeded"], 3)
        self.assertGreaterEqual(today_row["form_filled"], 1)

    def test_hours_window_excludes_old_jobs(self) -> None:
        old_id = self._enqueue("old", source="admin")
        self._claim()
        self.store.mark_job_succeeded(old_id, run_duration="1分0秒")
        old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        self._update(
            old_id,
            created_at=old_ts,
            started_at=old_ts,
            finished_at=old_ts,
            updated_at=old_ts,
        )

        new_id = self._enqueue("new", source="wework")
        self._claim()
        self.store.mark_job_succeeded(new_id, run_duration="2分0秒")

        win = self.store.pipeline_ops_stats(hours=24)
        self.assertEqual(win["extras"]["created"], 1)
        self.assertEqual(win["stages"]["register"]["success"], 1)

        all_stats = self.store.pipeline_ops_stats(hours=0)
        self.assertEqual(all_stats["extras"]["created"], 2)
        self.assertEqual(all_stats["stages"]["register"]["success"], 2)
        self.assertLessEqual(len(all_stats["daily"]), 31)


if __name__ == "__main__":
    unittest.main()
