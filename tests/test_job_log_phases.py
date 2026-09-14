"""步骤日志按 phase / 分隔行拆成注册、激活、NNC1。"""

from __future__ import annotations

import logging
import unittest

from src.storage.db import parse_job_result_messages
from src.wework.job_log_capture import (
    ACTIVATION_LOG_BANNER,
    NNC1_LOG_BANNER,
    JobLogCapture,
    split_job_log_phases,
)


def _line(msg: str, *, phase: str = "", level: str = "INFO") -> dict:
    row = {"level": level, "message": msg, "time": "09:00:00"}
    if phase:
        row["phase"] = phase
    return row


class TestParseKeepsPhase(unittest.TestCase):
    def test_parse_keeps_phase(self) -> None:
        parsed = parse_job_result_messages(
            [
                {"level": "INFO", "message": "a", "time": "01:00:00", "phase": "register"},
                {"level": "INFO", "message": "b", "phase": "activation"},
            ]
        )
        self.assertEqual(parsed[0]["phase"], "register")
        self.assertEqual(parsed[1]["phase"], "activation")


class TestJobLogCapturePhase(unittest.TestCase):
    def setUp(self) -> None:
        self._root = logging.getLogger()
        self._prev_level = self._root.level
        self._root.setLevel(logging.INFO)

    def tearDown(self) -> None:
        self._root.setLevel(self._prev_level)

    def test_capture_tags_phase(self) -> None:
        cap = JobLogCapture(phase="activation")
        cap.install()
        try:
            logging.getLogger("src.browser.icris_activation").info("opened")
        finally:
            cap.uninstall()
        snap = cap.snapshot()
        self.assertTrue(snap)
        self.assertEqual(snap[0]["phase"], "activation")
        self.assertEqual(snap[0]["message"], "opened")


class TestSplitJobLogPhases(unittest.TestCase):
    def test_phase_wins_over_order(self) -> None:
        lines = [
            _line("reg", phase="register"),
            _line("form first", phase="nnc1"),
            _line("act", phase="activation"),
        ]
        out = split_job_log_phases(lines)
        self.assertEqual([e["message"] for e in out["register"]], ["reg"])
        self.assertEqual([e["message"] for e in out["activation"]], ["act"])
        self.assertEqual([e["message"] for e in out["nnc1"]], ["form first"])

    def test_banner_fallback_and_drop(self) -> None:
        lines = [
            _line("register done"),
            _line(ACTIVATION_LOG_BANNER),
            _line("opened s06"),
            _line(NNC1_LOG_BANNER),
            _line("fill step1"),
        ]
        out = split_job_log_phases(lines)
        self.assertEqual([e["message"] for e in out["register"]], ["register done"])
        self.assertEqual([e["message"] for e in out["activation"]], ["opened s06"])
        self.assertEqual([e["message"] for e in out["nnc1"]], ["fill step1"])
        self.assertFalse(any(ACTIVATION_LOG_BANNER in e["message"] for e in out["activation"]))
        self.assertFalse(any(NNC1_LOG_BANNER in e["message"] for e in out["nnc1"]))

    def test_activation_retry_stays_in_activation(self) -> None:
        lines = [
            _line("reg done"),
            _line(ACTIVATION_LOG_BANNER),
            _line("try 1"),
            _line(ACTIVATION_LOG_BANNER),
            _line("try 2"),
        ]
        out = split_job_log_phases(lines)
        self.assertEqual([e["message"] for e in out["activation"]], ["try 1", "try 2"])
        self.assertEqual([e["message"] for e in out["register"]], ["reg done"])

    def test_last_error_form_failed(self) -> None:
        lines = [
            _line("reg done", phase="register"),
            _line("act ok", phase="activation"),
            _line("填表失败: boom", level="ERROR"),
        ]
        out = split_job_log_phases(lines, form_status="failed")
        self.assertEqual(out["nnc1"][-1]["message"], "填表失败: boom")
        self.assertFalse(any(e["message"] == "填表失败: boom" for e in out["register"]))

    def test_last_error_activation_failed(self) -> None:
        lines = [
            _line("reg done", phase="register"),
            _line("激活失败: bad", level="ERROR"),
        ]
        out = split_job_log_phases(lines, activation_status="failed")
        self.assertEqual(out["activation"][-1]["message"], "激活失败: bad")
        self.assertEqual([e["message"] for e in out["register"]], ["reg done"])


if __name__ == "__main__":
    unittest.main()
