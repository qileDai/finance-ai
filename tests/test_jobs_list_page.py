"""注册任务列表分页与瘦身，不影响详情 / 默认 list 行为。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.storage.db import ExternalGroupStore
from src.web.admin_api import handle_admin_api


class TestJobsListPage(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ExternalGroupStore(db_path=Path(self._tmp.name) / "jobs.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _enqueue(self, room: str, *, payload: dict | None = None) -> int:
        job, created = self.store.enqueue_registration_job(
            room, source="test", payload=payload or {"company_name_en": room}
        )
        self.assertTrue(created)
        return int(job["id"])

    def test_list_offset_and_count(self) -> None:
        ids = [self._enqueue(f"room-{i}") for i in range(15)]
        self.assertEqual(self.store.count_registration_jobs(), 15)
        page1 = self.store.list_registration_jobs(limit=10, offset=0)
        page2 = self.store.list_registration_jobs(limit=10, offset=10)
        self.assertEqual(len(page1), 10)
        self.assertEqual(len(page2), 5)
        self.assertEqual(int(page1[0]["id"]), ids[-1])
        self.assertEqual(int(page2[-1]["id"]), ids[0])

    def test_omit_result_messages(self) -> None:
        job_id = self._enqueue("room-log")
        claimed = self.store.claim_next_job()
        self.assertEqual(int(claimed["id"]), job_id)
        self.store.mark_job_failed(
            job_id,
            error="boom",
            result_messages=["step a", "step b"],
        )
        full = self.store.list_registration_jobs(limit=5)[0]
        self.assertIn("result_messages", full)
        self.assertTrue(str(full.get("result_messages") or ""))
        slim = self.store.list_registration_jobs(
            limit=5, omit_result_messages=True
        )[0]
        self.assertNotIn("result_messages", slim)
        self.assertIn("payload_json", slim)

    def test_admin_list_has_total_no_fields(self) -> None:
        for i in range(12):
            self._enqueue(f"api-{i}", payload={"company_name_en": f"Co {i}"})
        data, code = handle_admin_api(
            method="GET",
            path="/admin/api/jobs?limit=10&offset=0",
            store=self.store,
        )
        self.assertEqual(code, 200)
        self.assertTrue(data.get("ok"))
        self.assertEqual(data["limit"], 10)
        self.assertEqual(data["offset"], 0)
        self.assertEqual(data["total"], 12)
        self.assertEqual(len(data["items"]), 10)
        self.assertNotIn("fields", data["items"][0])
        self.assertNotIn("result_messages", data["items"][0])

        page2, code2 = handle_admin_api(
            method="GET",
            path="/admin/api/jobs?limit=10&offset=10",
            store=self.store,
        )
        self.assertEqual(code2, 200)
        self.assertEqual(len(page2["items"]), 2)

    def test_screenshot_path_lookup(self) -> None:
        job_id = self._enqueue("shot")
        found, path = self.store.get_job_screenshot_path(job_id, "esubmit")
        self.assertTrue(found)
        self.assertEqual(path, "")
        missing, _ = self.store.get_job_screenshot_path(99999, "fail")
        self.assertFalse(missing)


if __name__ == "__main__":
    unittest.main()
