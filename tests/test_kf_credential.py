"""微信客服 40001 凭证检测。"""

from __future__ import annotations

import unittest

from src.wework.external_client import is_wework_invalid_credential


class TestKfInvalidCredential(unittest.TestCase):
    def test_detects_40001(self):
        msg = (
            "获取微信客服 access_token 失败: "
            "{'errcode': 40001, 'errmsg': 'invalid credential'}"
        )
        self.assertTrue(is_wework_invalid_credential(msg))
        self.assertTrue(is_wework_invalid_credential(RuntimeError(msg)))

    def test_ignores_other_errors(self):
        self.assertFalse(is_wework_invalid_credential("timeout"))
        self.assertFalse(is_wework_invalid_credential("kf/sync_msg 失败: {'errcode': 40014}"))


if __name__ == "__main__":
    unittest.main()
