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
        self.assertNotIn("payload_json", data["items"][0])
        self.assertEqual(data["items"][0]["company_name_en"], "Co 11")

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
        self.store.set_job_activation_screenshot(job_id, "/tmp/act.png")
        found_act, act_path = self.store.get_job_screenshot_path(job_id, "activation")
        self.assertTrue(found_act)
        self.assertEqual(act_path, "/tmp/act.png")
        missing, _ = self.store.get_job_screenshot_path(99999, "fail")
        self.assertFalse(missing)

    def test_slim_list_extracts_payload_keeps_default_full(self) -> None:
        payload = {
            "company_name_cn": "测试公司",
            "company_name_en": "Test Co Ltd",
            "directors": [
                {
                    "name_en": "CHAN TAI MAN",
                    "id_type": "HKID",
                    "id_number": "A123456(7)",
                }
            ],
            "applicant": {"email": "a@x.com", "phone": "12345678"},
            "icris_account": {"username": "user1", "password": "pass1"},
            "contact": {"email": "c@x.com"},
            "identity_proof": {"id_type": "PASSPORT"},
        }
        self._enqueue("room-slim", payload=payload)
        default = self.store.list_registration_jobs(limit=1)[0]
        self.assertIn("payload_json", default)
        self.assertIn("package_dir", default)
        slim = self.store.list_registration_jobs(limit=1, slim_list=True)[0]
        self.assertNotIn("payload_json", slim)
        self.assertNotIn("result_messages", slim)
        self.assertNotIn("package_dir", slim)
        self.assertEqual(slim["company_name_cn"], "测试公司")
        self.assertEqual(slim["company_name_en"], "Test Co Ltd")
        self.assertEqual(slim["director_name"], "CHAN TAI MAN")
        self.assertEqual(slim["id_type"], "HKID")
        self.assertEqual(slim["id_number"], "A123456(7)")
        self.assertEqual(slim["icris_username"], "user1")
        self.assertEqual(slim["icris_password"], "pass1")
        self.assertEqual(slim["contact_email"], "c@x.com")
        self.assertEqual(slim["contact_phone"], "12345678")

        data, code = handle_admin_api(
            method="GET",
            path="/admin/api/jobs?limit=5",
            store=self.store,
        )
        self.assertEqual(code, 200)
        row = data["items"][0]
        self.assertEqual(row["director_name"], "CHAN TAI MAN")
        self.assertEqual(row["id_type"], "香港身份证")
        self.assertNotIn("payload_json", row)

    def test_screenshot_thumb_jpeg_cached_original_png_unchanged(self) -> None:
        import os
        import time

        from PIL import Image

        from src.web.admin_server import (
            ensure_job_screenshot_thumb,
            job_screenshot_thumb_path,
            read_job_screenshot_file,
        )

        png = Path(self._tmp.name) / "shot.png"
        Image.new("RGB", (1600, 1000), (30, 80, 200)).save(
            png, "PNG", compress_level=0
        )
        orig, ctype, cache, suffix = read_job_screenshot_file(png, thumb=False)
        self.assertEqual(ctype, "image/png")
        self.assertEqual(cache, "private, no-cache")
        self.assertEqual(suffix, ".png")
        self.assertGreater(len(orig), 1000)

        thumb, tctype, tcache, tsuffix = read_job_screenshot_file(png, thumb=True)
        self.assertEqual(tctype, "image/jpeg")
        self.assertEqual(tcache, "private, max-age=86400")
        self.assertEqual(tsuffix, ".thumb.jpg")
        self.assertLess(len(thumb), len(orig))
        dest = job_screenshot_thumb_path(png)
        self.assertTrue(dest.is_file())
        before = dest.read_bytes()

        Image.new("RGB", (1600, 1000), (200, 20, 20)).save(
            png, "PNG", compress_level=0
        )
        future = time.time() + 10
        os.utime(png, (future, future))
        ensure_job_screenshot_thumb(png)
        self.assertNotEqual(before, dest.read_bytes())

    def _delete(self, job_id: int) -> tuple[dict, int]:
        return handle_admin_api(
            method="DELETE",
            path=f"/admin/api/jobs/{job_id}",
            store=self.store,
        )

    def test_delete_failed_cancelled_rejected(self) -> None:
        failed_id = self._enqueue("room-failed")
        self.store.mark_job_failed(failed_id, error="boom")
        data, code = self._delete(failed_id)
        self.assertEqual(code, 200)
        self.assertTrue(data.get("ok"))
        self.assertEqual(self.store.count_registration_jobs(), 0)

        cancelled_id = self._enqueue("room-cancelled")
        self.store.cancel_registration_job(cancelled_id)
        data, code = self._delete(cancelled_id)
        self.assertEqual(code, 200)
        self.assertEqual(self.store.count_registration_jobs(), 0)

        rejected_id = self._enqueue("room-rejected")
        claimed = self.store.claim_next_job()
        self.assertEqual(int(claimed["id"]), rejected_id)
        self.store.mark_job_awaiting_review(rejected_id)
        rejected = self.store.reject_job_submit(rejected_id)
        self.assertEqual(str(rejected.get("status")), "failed")
        self.assertEqual(str(rejected.get("review_status")), "rejected")
        data, code = self._delete(rejected_id)
        self.assertEqual(code, 200)
        self.assertEqual(self.store.count_registration_jobs(), 0)

    def test_delete_running_pending_succeeded_review_rejected(self) -> None:
        pending_id = self._enqueue("room-pending")
        data, code = self._delete(pending_id)
        self.assertEqual(code, 409)
        self.assertFalse(data.get("ok"))
        self.assertIsNotNone(self.store.get_registration_job(pending_id))

        running_id = self._enqueue("room-running")
        claimed = self.store.claim_next_job()
        self.assertEqual(int(claimed["id"]), pending_id)
        claimed2 = self.store.claim_next_job()
        self.assertEqual(int(claimed2["id"]), running_id)
        data, code = self._delete(running_id)
        self.assertEqual(code, 409)
        self.assertIsNotNone(self.store.get_registration_job(running_id))

        review_id = self._enqueue("room-review")
        claimed3 = self.store.claim_next_job()
        self.assertEqual(int(claimed3["id"]), review_id)
        self.store.mark_job_awaiting_review(review_id)
        data, code = self._delete(review_id)
        self.assertEqual(code, 409)
        self.assertIsNotNone(self.store.get_registration_job(review_id))

        ok_id = self._enqueue("room-ok")
        claimed4 = self.store.claim_next_job()
        self.assertEqual(int(claimed4["id"]), ok_id)
        self.store.mark_job_succeeded(ok_id)
        data, code = self._delete(ok_id)
        self.assertEqual(code, 409)
        self.assertIsNotNone(self.store.get_registration_job(ok_id))

    def test_delete_missing_job(self) -> None:
        data, code = self._delete(99999)
        self.assertEqual(code, 404)
        self.assertFalse(data.get("ok"))


if __name__ == "__main__":
    unittest.main()
