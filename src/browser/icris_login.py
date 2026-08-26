"""向后兼容：请使用 icris_nnc1_form.IcrisNnc1FormBot（阶段 3 NNC1 填表）。"""

from __future__ import annotations

from src.browser.icris_nnc1_form import IcrisNnc1FormBot, LOGIN_URL

IcrisLoginBot = IcrisNnc1FormBot

__all__ = ["IcrisLoginBot", "IcrisNnc1FormBot", "LOGIN_URL"]
