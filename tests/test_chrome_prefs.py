"""Chrome profile prefs / 启动参数：关掉翻译条与保存密码气泡。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.browser.launcher import (
    _CHROME_DISABLE_FEATURES,
    _chromium_launch_args,
    _ensure_chrome_profile_automation_prefs,
)


class TestChromeAutomationPrefs(unittest.TestCase):
    def test_writes_translate_and_password_prefs(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp)
            _ensure_chrome_profile_automation_prefs(profile)
            prefs = json.loads(
                (profile / "Default" / "Preferences").read_text(encoding="utf-8")
            )
        self.assertFalse(prefs["credentials_enable_service"])
        self.assertFalse(prefs["profile"]["password_manager_enabled"])
        self.assertFalse(prefs["profile"]["password_manager_leak_detection"])
        self.assertFalse(prefs["translate"]["enabled"])

    def test_launch_args_disable_password_bubble(self):
        args = _chromium_launch_args()
        self.assertIn("--disable-save-password-bubble", args)
        features = next(a for a in args if a.startswith("--disable-features="))
        self.assertIn("Translate", features)
        self.assertIn("PasswordManagerOnboarding", features)
        self.assertIn("PasswordLeakDetection", features)
        self.assertEqual(features, f"--disable-features={_CHROME_DISABLE_FEATURES}")


if __name__ == "__main__":
    unittest.main()
