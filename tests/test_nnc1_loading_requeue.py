"""NNC1 载入中超时：失败关浏览器、同 job 限一次插队。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.browser.icris_errors import (
    IcrisLoadingTimeoutError,
    NNC1_LOADING_TIMEOUT_MSG,
    is_nnc1_loading_timeout,
)
from src.browser.icris_nnc1_form import IcrisNnc1FormBot
from src.browser.icris_ui_common import wait_after_nav_loading
from src.storage.db import ExternalGroupStore


class TestWaitAfterNavLoadingTimeout(unittest.IsolatedAsyncioTestCase):
    async def test_raises_when_overlay_stuck(self):
        page = MagicMock()
        with (
            patch(
                "src.browser.icris_ui_common.is_page_loading",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "src.browser.icris_ui_common.wait_spin_clear",
                new=AsyncMock(return_value=False),
            ),
        ):
            with self.assertRaises(IcrisLoadingTimeoutError) as ctx:
                await wait_after_nav_loading(page, timeout_ms=1, appear_ms=0)
        self.assertTrue(is_nnc1_loading_timeout(ctx.exception))
        self.assertIn("载入中超时", str(ctx.exception))

    async def test_returns_when_overlay_clears(self):
        page = MagicMock()
        with patch(
            "src.browser.icris_ui_common.wait_spin_clear",
            new=AsyncMock(return_value=True),
        ):
            await wait_after_nav_loading(page, timeout_ms=1, appear_ms=0)


class TestNnc1LoadingRequeueStore(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ExternalGroupStore(
            db_path=Path(self._tmp.name) / "loading_requeue.db"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _pending_form_job(self, room: str) -> int:
        job, created = self.store.enqueue_registration_job(room, source="test")
        self.assertTrue(created)
        job_id = int(job["id"])
        self.store.mark_job_activated(job_id)
        return job_id

    def test_boosted_job_ahead_of_smaller_id(self):
        first = self._pending_form_job("room-old")
        second = self._pending_form_job("room-boost")
        self.assertLess(first, second)
        self.store.mark_job_form_failed(second, f"填表失败: {NNC1_LOADING_TIMEOUT_MSG}")
        boosted = self.store.requeue_job_form_loading_timeout(second)
        self.assertIsNotNone(boosted)
        self.assertEqual(boosted["form_status"], "pending")
        self.assertGreaterEqual(int(boosted["form_boost"] or 0), 1)
        pending = self.store.get_jobs_pending_form()
        ids = [int(j["id"]) for j in pending]
        self.assertEqual(ids[0], second)
        self.assertEqual(ids[1], first)

    def test_second_loading_timeout_stays_failed(self):
        job_id = self._pending_form_job("room-twice")
        self.store.mark_job_form_failed(job_id, f"填表失败: {NNC1_LOADING_TIMEOUT_MSG}")
        self.assertIsNotNone(self.store.requeue_job_form_loading_timeout(job_id))
        self.store.mark_job_form_failed(job_id, f"填表失败: {NNC1_LOADING_TIMEOUT_MSG}")
        self.assertIsNone(self.store.requeue_job_form_loading_timeout(job_id))
        row = self.store.get_registration_job(job_id)
        self.assertEqual(row["form_status"], "failed")
        pending_ids = [int(j["id"]) for j in self.store.get_jobs_pending_form()]
        self.assertNotIn(job_id, pending_ids)

    def test_bot_persist_requeues_once_then_keeps_failed(self):
        job_id = self._pending_form_job("room-persist")
        bot = IcrisNnc1FormBot()
        bot.job_id = job_id
        bot.persist_form_outcome(False, NNC1_LOADING_TIMEOUT_MSG, store=self.store)
        row = self.store.get_registration_job(job_id)
        self.assertEqual(row["form_status"], "pending")
        self.assertGreaterEqual(int(row["form_boost"] or 0), 1)
        self.assertEqual(int(row["nnc1_loading_retries"] or 0), 1)

        bot._form_persisted = False
        bot.persist_form_outcome(False, NNC1_LOADING_TIMEOUT_MSG, store=self.store)
        row = self.store.get_registration_job(job_id)
        self.assertEqual(row["form_status"], "failed")
        pending_ids = [int(j["id"]) for j in self.store.get_jobs_pending_form()]
        self.assertNotIn(job_id, pending_ids)
