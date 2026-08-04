"""ICRIS 流程异常（可携带失败截图路径）"""

from __future__ import annotations


class IcrisFlowError(RuntimeError):
    def __init__(self, message: str, *, screenshot_path: str = "") -> None:
        super().__init__(message)
        self.screenshot_path = screenshot_path or ""
