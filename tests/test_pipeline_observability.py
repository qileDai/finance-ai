"""全流程监控旁路：心跳文件、运营页 health、不改任务状态。"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src.storage.db import ExternalGroupStore
from src.web.admin_api import handle_admin_api
from src.wework.worker_heartbeat import read_worker_heartbeat, touch_worker_heartbeat


class TestWorkerHeartbeatFile(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "icris-worker.heartbeat.json"
        self._patch = patch("src.wework.worker_heartbeat.HEARTBEAT_PATH", self.path)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmp.cleanup()

    def test_touch_and_read_fresh(self) -> None:
        self.assertFalse(self.path.exists())
        empty = read_worker_heartbeat(stale_after_s=120)
        self.assertFalse(empty["present"])
        self.assertTrue(empty["stale"])

        touch_worker_heartbeat("register")
        touch_worker_heartbeat("form")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIn("register", data)
        self.assertIn("form", data)
        fresh = read_worker_heartbeat(stale_after_s=120)
        self.assertTrue(fresh["present"])
        self.assertFalse(fresh["stale"])
        self.assertGreaterEqual(fresh["age_seconds"], 0)

    def test_stale_when_old(self) -> None:
        touch_worker_heartbeat("activation")
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["ts"] = time.time() - 600
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        stale = read_worker_heartbeat(stale_after_s=120)
        self.assertTrue(stale["present"])
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["label"], "异常")


class TestOverviewHealthApi(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ExternalGroupStore(db_path=Path(self._tmp.name) / "ops.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_overview_includes_health_and_extra_backlog(self) -> None:
        data, code = handle_admin_api(
            method="GET",
            path="/admin/api/overview?hours=24",
            store=self.store,
        )
        self.assertEqual(code, 200)
        self.assertTrue(data.get("ok"))
        backlog = data["backlog"]
        for key in (
            "register_pending",
            "register_running",
            "awaiting_review",
            "activation_pending",
            "form_pending",
            "activation_pending_no_url",
            "activation_pending_has_url",
            "activation_failed",
            "form_failed",
        ):
            self.assertIn(key, backlog)
            self.assertEqual(backlog[key], 0)
        health = data["health"]
        self.assertIn("worker", health)
        self.assertIn("cdp", health)
        self.assertIn("present", health["worker"])
        self.assertIn("stale", health["worker"])
        self.assertIn("label", health["cdp"])
        self.assertIn("busy", health["cdp"])


if __name__ == "__main__":
    unittest.main()
