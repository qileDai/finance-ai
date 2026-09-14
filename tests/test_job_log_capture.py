"""JobLogCapture 前缀过滤与 JobLogSession 追加写回。"""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from src.storage.db import ExternalGroupStore, parse_job_result_messages
from src.wework.job_log_capture import (
    ACTIVATION_LOG_PREFIXES,
    JobLogCapture,
    JobLogSession,
)


class _MemStore:
    def __init__(self, existing: list[dict] | None = None) -> None:
        self.saved = list(existing or [])
        self.writes: list[list[dict]] = []

    def get_job_result_messages(self, job_id: int) -> list[dict]:
        return [dict(x) for x in self.saved]

    def update_job_result_messages(self, job_id: int, messages: list) -> None:
        self.writes.append([dict(x) for x in messages])


class TestParseJobResultMessages(unittest.TestCase):
    def test_dict_and_plain_string(self) -> None:
        raw = [
            {"level": "INFO", "message": "a", "time": "01:00:00"},
            "plain",
            {"level": "ERROR", "message": "boom"},
        ]
        out = parse_job_result_messages(raw)
        self.assertEqual(out[0]["message"], "a")
        self.assertEqual(out[1], {"level": "INFO", "message": "plain"})
        self.assertEqual(out[2]["level"], "ERROR")

    def test_json_string_and_empty(self) -> None:
        self.assertEqual(parse_job_result_messages(""), [])
        self.assertEqual(parse_job_result_messages("[]"), [])
        parsed = parse_job_result_messages(
            '[{"level":"WARNING","message":"w","time":"02:00:00"}]'
        )
        self.assertEqual(parsed[0]["level"], "WARNING")
        self.assertEqual(parsed[0]["time"], "02:00:00")


class TestJobLogCaptureFilter(unittest.TestCase):
    def setUp(self) -> None:
        self._root = logging.getLogger()
        self._prev_level = self._root.level
        self._root.setLevel(logging.INFO)

    def tearDown(self) -> None:
        self._root.setLevel(self._prev_level)

    def test_name_prefixes_drops_registration(self) -> None:
        cap = JobLogCapture(name_prefixes=ACTIVATION_LOG_PREFIXES)
        cap.install()
        try:
            logging.getLogger("src.browser.icris_activation").info("act-line")
            logging.getLogger("src.browser.icris_registration").info("reg-line")
            logging.getLogger("src.email.imap_client").info("mail-line")
        finally:
            cap.uninstall()
        msgs = [e["message"] for e in cap.snapshot()]
        self.assertIn("act-line", msgs)
        self.assertIn("mail-line", msgs)
        self.assertNotIn("reg-line", msgs)

    def test_no_prefix_keeps_all_src(self) -> None:
        cap = JobLogCapture()
        cap.install()
        try:
            logging.getLogger("src.browser.icris_registration").info("reg-all")
        finally:
            cap.uninstall()
        self.assertIn("reg-all", [e["message"] for e in cap.snapshot()])


class TestJobLogSessionAppend(unittest.TestCase):
    def setUp(self) -> None:
        self._root = logging.getLogger()
        self._prev_level = self._root.level
        self._root.setLevel(logging.INFO)

    def tearDown(self) -> None:
        self._root.setLevel(self._prev_level)

    def test_prefix_plus_snapshot_no_duplicate_on_flush(self) -> None:
        store = _MemStore(
            [{"level": "INFO", "message": "reg step", "time": "09:00:00"}]
        )
        session = JobLogSession(
            store,
            7,
            name_prefixes=ACTIVATION_LOG_PREFIXES,
            banner="—— 账号激活开始 ——",
            phase="activation",
        )
        with session:
            logging.getLogger("src.wework.icris_activation_worker").info("act running")
            session.flush(force=True)
            session.flush(force=True)
        first = store.writes[0]
        last = store.writes[-1]
        self.assertEqual(sum(1 for e in last if e.get("message") == "reg step"), 1)
        self.assertTrue(any("账号激活开始" in str(e.get("message")) for e in last))
        self.assertTrue(any("act running" in str(e.get("message")) for e in last))
        self.assertEqual(
            sum(1 for e in first if e.get("message") == "reg step"),
            1,
        )

    def test_persists_onto_registration_job(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = ExternalGroupStore(db_path=Path(tmp.name) / "jobs.db")
        job, created = store.enqueue_registration_job(
            "room-log", source="test", payload={"company_name_en": "Co"}
        )
        self.assertTrue(created)
        job_id = int(job["id"])
        claimed = store.claim_next_job()
        self.assertIsNotNone(claimed)
        store.mark_job_succeeded(
            job_id,
            result_messages=[
                {"level": "INFO", "message": "register done", "time": "08:00:00"}
            ],
        )
        with JobLogSession(
            store,
            job_id,
            name_prefixes=ACTIVATION_LOG_PREFIXES,
            banner="—— 账号激活开始 ——",
            phase="activation",
        ):
            logging.getLogger("src.browser.icris_activation").info("opened s06")
        msgs = store.get_job_result_messages(job_id)
        texts = [e["message"] for e in msgs]
        self.assertEqual(texts[0], "register done")
        self.assertIn("—— 账号激活开始 ——", texts)
        self.assertIn("opened s06", texts)
        act = [e for e in msgs if e.get("message") == "opened s06"]
        self.assertEqual(act[0].get("phase"), "activation")


if __name__ == "__main__":
    unittest.main()
