"""registration_jobs 到 s03a / 整个任务 / NNC1 耗时。"""

from __future__ import annotations

import re
import tempfile
import time
import unittest
from pathlib import Path

from src.storage.db import (
    ExternalGroupStore,
    duration_since_started,
    format_job_run_duration,
)

_DURATION_RE = re.compile(r"^\d+分\d+秒$")


def _duration_seconds(text: str) -> int:
    minutes, rest = text.split("分", 1)
    return int(minutes) * 60 + int(rest.replace("秒", ""))


class TestFormatJobRunDuration(unittest.TestCase):
    def test_zero_and_rounding(self):
        self.assertEqual(format_job_run_duration(0), "0分0秒")
        self.assertEqual(format_job_run_duration(0.4), "0分0秒")
        self.assertEqual(format_job_run_duration(0.6), "0分1秒")

    def test_under_one_minute(self):
        self.assertEqual(format_job_run_duration(41), "0分41秒")

    def test_minutes_and_seconds(self):
        self.assertEqual(format_job_run_duration(323), "5分23秒")

    def test_long_run(self):
        self.assertEqual(format_job_run_duration(70 * 60), "70分0秒")

    def test_negative_clamped(self):
        self.assertEqual(format_job_run_duration(-3), "0分0秒")

    def test_duration_since_started(self):
        self.assertEqual(
            duration_since_started(
                "2026-09-07T00:00:00+00:00",
                "2026-09-07T00:05:23+00:00",
            ),
            "5分23秒",
        )
        self.assertEqual(duration_since_started("", "2026-09-07T00:00:00+00:00"), "")


class TestJobRunDurationStore(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ExternalGroupStore(
            db_path=Path(self._tmp.name) / "duration.db"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _enqueued_id(self, room: str) -> int:
        job, created = self.store.enqueue_registration_job(room, source="test")
        self.assertTrue(created)
        return int(job["id"])

    def test_succeed_writes_run_duration(self):
        job_id = self._enqueued_id("room-ok")
        claimed = self.store.claim_next_job()
        self.assertEqual(int(claimed["id"]), job_id)
        self.assertEqual(str(claimed.get("run_duration") or ""), "")
        self.store.mark_job_succeeded(job_id, run_duration="0分41秒")
        row = self.store.get_registration_job(job_id)
        self.assertEqual(row["run_duration"], "0分41秒")

    def test_fail_requeue_keeps_this_run_then_claim_clears(self):
        job_id = self._enqueued_id("room-retry")
        claimed = self.store.claim_next_job()
        self.assertEqual(int(claimed["id"]), job_id)
        self.store.mark_job_failed(
            job_id,
            error="tmp",
            requeue=True,
            available_at="",
            run_duration="5分23秒",
        )
        row = self.store.get_registration_job(job_id)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["run_duration"], "5分23秒")
        claimed2 = self.store.claim_next_job()
        self.assertEqual(int(claimed2["id"]), job_id)
        self.assertEqual(str(claimed2.get("run_duration") or ""), "")
        self.assertEqual(str(claimed2.get("s03a_duration") or ""), "")

    def test_admin_requeue_clears_run_duration(self):
        job_id = self._enqueued_id("room-reset")
        claimed = self.store.claim_next_job()
        self.store.mark_job_failed(
            int(claimed["id"]),
            error="done",
            requeue=False,
            run_duration="1分2秒",
        )
        row = self.store.get_registration_job(job_id)
        self.assertEqual(row["run_duration"], "1分2秒")
        reset = self.store.requeue_registration_job(job_id)
        self.assertEqual(str(reset.get("run_duration") or ""), "")
        self.assertEqual(str(reset.get("s03a_duration") or ""), "")

    def test_s03a_duration_excludes_review_wait(self):
        job_id = self._enqueued_id("room-s03a")
        self.store.claim_next_job()
        self.store.update_job_esubmit_screenshot(job_id, "/tmp/esubmit.png")
        self.store.set_job_s03a_duration(job_id)
        row = self.store.get_registration_job(job_id)
        first = str(row["s03a_duration"] or "")
        self.assertRegex(first, _DURATION_RE)
        time.sleep(1.2)
        self.store.set_job_s03a_duration(job_id)
        row2 = self.store.get_registration_job(job_id)
        self.assertEqual(row2["s03a_duration"], first)
        self.assertEqual(str(row2.get("run_duration") or ""), "")

    def test_awaiting_review_then_reject_fills_total(self):
        job_id = self._enqueued_id("room-reject")
        self.store.claim_next_job()
        self.store.mark_job_awaiting_review(job_id)
        row = self.store.get_registration_job(job_id)
        s03a = str(row["s03a_duration"] or "")
        self.assertRegex(s03a, _DURATION_RE)
        time.sleep(1.2)
        rejected = self.store.reject_job_submit(job_id)
        self.assertEqual(rejected["status"], "failed")
        self.assertEqual(rejected["s03a_duration"], s03a)
        total = str(rejected["run_duration"] or "")
        self.assertRegex(total, _DURATION_RE)
        self.assertGreaterEqual(_duration_seconds(total), _duration_seconds(s03a))
        self.assertGreaterEqual(_duration_seconds(total), 1)

    def test_fail_orphan_fills_run_duration(self):
        job_id = self._enqueued_id("room-orphan")
        self.store.claim_next_job()
        self.store.mark_job_awaiting_review(job_id)
        n = self.store.fail_orphan_awaiting_review()
        self.assertEqual(n, 1)
        row = self.store.get_registration_job(job_id)
        self.assertEqual(row["status"], "failed")
        self.assertRegex(str(row["s03a_duration"] or ""), _DURATION_RE)
        self.assertRegex(str(row["run_duration"] or ""), _DURATION_RE)

    def test_admin_requeue_clears_s03a_duration(self):
        job_id = self._enqueued_id("room-reset-s03a")
        self.store.claim_next_job()
        self.store.set_job_s03a_duration(job_id)
        self.store.mark_job_failed(
            job_id,
            error="done",
            requeue=False,
            run_duration="1分2秒",
        )
        row = self.store.get_registration_job(job_id)
        self.assertRegex(str(row["s03a_duration"] or ""), _DURATION_RE)
        reset = self.store.requeue_registration_job(job_id)
        self.assertEqual(str(reset.get("s03a_duration") or ""), "")
        self.assertEqual(str(reset.get("run_duration") or ""), "")
        self.assertEqual(str(reset.get("nnc1_duration") or ""), "")


class TestNnc1DurationStore(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ExternalGroupStore(
            db_path=Path(self._tmp.name) / "nnc1_duration.db"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _enqueued_id(self, room: str) -> int:
        job, created = self.store.enqueue_registration_job(room, source="test")
        self.assertTrue(created)
        return int(job["id"])

    def test_form_filled_writes_nnc1_duration(self):
        job_id = self._enqueued_id("room-nnc1-ok")
        self.store.mark_job_form_filled(
            job_id, "/tmp/form.png", nnc1_duration="3分12秒"
        )
        row = self.store.get_registration_job(job_id)
        self.assertEqual(row["form_status"], "filled")
        self.assertEqual(row["nnc1_duration"], "3分12秒")

    def test_form_failed_writes_duration_and_retry_clears(self):
        job_id = self._enqueued_id("room-nnc1-fail")
        self.store.mark_job_form_failed(
            job_id,
            "填表失败: 初步检查拒绝: 名称相同",
            nnc1_duration="2分5秒",
        )
        row = self.store.get_registration_job(job_id)
        self.assertEqual(row["form_status"], "failed")
        self.assertEqual(row["nnc1_duration"], "2分5秒")
        self.assertIn("初步检查拒绝", row["last_error"])
        reset = self.store.reset_job_form_retry(job_id)
        self.assertEqual(reset["form_status"], "pending")
        self.assertEqual(str(reset.get("nnc1_duration") or ""), "")
        self.assertEqual(str(reset.get("last_error") or ""), "")

    def test_requeue_and_claim_clear_nnc1_duration(self):
        job_id = self._enqueued_id("room-nnc1-requeue")
        self.store.claim_next_job()
        self.store.mark_job_form_filled(
            job_id, "/tmp/form.png", nnc1_duration="1分1秒"
        )
        self.store.mark_job_failed(
            job_id, error="done", requeue=False, run_duration="1分2秒"
        )
        reset = self.store.requeue_registration_job(job_id)
        self.assertEqual(str(reset.get("nnc1_duration") or ""), "")
        claimed = self.store.claim_next_job()
        self.assertEqual(int(claimed["id"]), job_id)
        self.assertEqual(str(claimed.get("nnc1_duration") or ""), "")

    def test_form_error_keeps_long_reject_reason(self):
        job_id = self._enqueued_id("room-long-err")
        reason = "填表失败: 初步检查拒绝: " + ("名稱相同。" * 200)
        self.assertGreater(len(reason), 500)
        self.store.mark_job_form_failed(job_id, reason)
        row = self.store.get_registration_job(job_id)
        self.assertGreater(len(row["last_error"]), 500)
        self.assertTrue(str(row["last_error"]).startswith("填表失败"))
        self.assertLessEqual(len(row["last_error"]), 2000)

    def test_bot_persist_writes_without_waiting_keep_browser(self):
        from src.browser.icris_nnc1_form import IcrisNnc1FormBot

        job_id = self._enqueued_id("room-persist-keep")
        bot = IcrisNnc1FormBot()
        bot.job_id = job_id
        bot._nnc1_t0 = time.monotonic() - 65
        bot._job_screenshot_path = "/tmp/form.png"
        bot.persist_form_outcome(
            False, "初步检查拒绝: 名稱相同", store=self.store
        )
        self.assertTrue(self.store.job_form_outcome_written(job_id))
        row = self.store.get_registration_job(job_id)
        self.assertEqual(row["form_status"], "failed")
        self.assertIn("名稱相同", row["last_error"])
        self.assertRegex(str(row["nnc1_duration"] or ""), _DURATION_RE)
        bot.persist_form_outcome(True, store=self.store)
        row2 = self.store.get_registration_job(job_id)
        self.assertEqual(row2["form_status"], "failed")

    def test_keep_browser_wait_is_after_bot_run(self):
        import inspect

        from src.browser.icris_nnc1_form import IcrisNnc1FormBot
        from src.workflow.steps import RegistrationWorkflow

        run_src = inspect.getsource(IcrisNnc1FormBot.run)
        self.assertNotIn("while True", run_src)
        self.assertIn("persist_form_outcome", run_src)
        steps_src = inspect.getsource(RegistrationWorkflow.step_icris_login)
        self.assertIn("asyncio.sleep(3600)", steps_src)
        self.assertIn("keep_browser", steps_src)
        self.assertIn("job_form_outcome_written", steps_src)


if __name__ == "__main__":
    unittest.main()
