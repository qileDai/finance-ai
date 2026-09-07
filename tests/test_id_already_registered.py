"""s04 证件号已在系统登记：识别、入库、快速注册拦截。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.browser.icris_errors import (
    IcrisFlowError,
    id_numbers_from_job_payload,
    is_id_already_registered_error,
    normalize_id_number_key,
)
from src.storage.db import ExternalGroupStore


class TestIdAlreadyRegisteredDetect(unittest.TestCase):
    def test_traditional_and_simplified(self):
        self.assertTrue(
            is_id_already_registered_error(
                ["相同的身分證件號碼已在系統中登記！"]
            )
        )
        self.assertTrue(
            is_id_already_registered_error("相同的身份证件号码已在系统中登记")
        )
        self.assertTrue(
            is_id_already_registered_error("相同的身分證明號碼已在系統中登記！")
        )
        self.assertTrue(
            is_id_already_registered_error("相同的身份证明号码已在系统中登记")
        )

    def test_body_inner_text_with_page_chrome(self):
        page_text = (
            "用戶登記\n步驟3 - 提交身分證明\n"
            "中華人民共和國身分證號碼\n44051420000318492X\n"
            "相同的身分證明號碼已在系統中登記！\n"
            "證明文件\n網上提交\n載入中..."
        )
        self.assertTrue(is_id_already_registered_error(page_text))

    def test_other_validation_ignored(self):
        self.assertFalse(is_id_already_registered_error(["請輸入身分證號碼"]))
        self.assertFalse(is_id_already_registered_error([]))
        self.assertFalse(is_id_already_registered_error(None))
        self.assertFalse(
            is_id_already_registered_error(
                "用戶登記\n步驟3 - 提交身分證明\n請輸入身分證號碼\n載入中..."
            )
        )

    def test_normalize_key(self):
        self.assertEqual(
            normalize_id_number_key(" 44051420000318492x "),
            "44051420000318492X",
        )
        self.assertEqual(
            normalize_id_number_key("F570235（2）"),
            "F570235(2)",
        )

    def test_payload_id_numbers(self):
        nums = id_numbers_from_job_payload(
            {
                "applicant": {"id_number": "44051420000318492X"},
                "directors": [{"id_number": "A123"}],
            }
        )
        self.assertIn("44051420000318492X", nums)
        self.assertIn("A123", nums)

    def test_flow_error_flags(self):
        err = IcrisFlowError(
            "相同的身分證件號碼已在系統中登記！",
            no_requeue=True,
            id_already_registered=True,
        )
        self.assertTrue(err.no_requeue)
        self.assertTrue(err.id_already_registered)


class TestIdAlreadyRegisteredStore(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ExternalGroupStore(
            db_path=Path(self._tmp.name) / "id_dup.db"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_find_after_mark(self):
        job, created = self.store.enqueue_registration_job(
            "room-dup",
            source="test",
            payload={"applicant": {"id_number": "44051420000318492X"}},
        )
        self.assertTrue(created)
        job_id = int(job["id"])
        self.assertIsNone(
            self.store.find_job_id_already_registered("44051420000318492X")
        )
        self.store.mark_job_id_already_registered(job_id)
        found = self.store.find_job_id_already_registered("44051420000318492x")
        self.assertIsNotNone(found)
        self.assertEqual(int(found["id"]), job_id)
        self.assertEqual(int(found["id_already_registered"]), 1)

    def test_claim_keeps_flag(self):
        job, _ = self.store.enqueue_registration_job(
            "room-keep",
            source="test",
            payload={"applicant": {"id_number": "A111111(1)"}},
        )
        job_id = int(job["id"])
        self.store.mark_job_id_already_registered(job_id)
        self.store.mark_job_failed(job_id, error="dup", requeue=False)
        claimed = self.store.claim_next_job()
        self.assertIsNone(claimed)
        found = self.store.find_job_id_already_registered("A111111(1)")
        self.assertIsNotNone(found)
        self.assertEqual(int(found["id_already_registered"]), 1)

    def test_requeue_keeps_flag(self):
        job, _ = self.store.enqueue_registration_job(
            "room-rq",
            source="test",
            payload={"applicant": {"id_number": "B222222(2)"}},
        )
        job_id = int(job["id"])
        claimed = self.store.claim_next_job()
        self.assertEqual(int(claimed["id"]), job_id)
        self.store.mark_job_id_already_registered(job_id)
        self.store.mark_job_failed(job_id, error="dup", requeue=False)
        reset = self.store.requeue_registration_job(job_id)
        self.assertEqual(int(reset["id_already_registered"]), 1)
        self.assertIsNotNone(self.store.find_job_id_already_registered("B222222(2)"))
