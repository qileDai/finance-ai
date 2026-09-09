"""ICRIS 流程异常（可携带失败截图路径）"""

from __future__ import annotations

import re
import unicodedata
from typing import Any


class IcrisFlowError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        screenshot_path: str = "",
        no_requeue: bool = False,
        id_already_registered: bool = False,
    ) -> None:
        super().__init__(message)
        self.screenshot_path = screenshot_path or ""
        self.no_requeue = no_requeue
        self.id_already_registered = bool(id_already_registered)


class IcrisStepLoadError(RuntimeError):
    """步骤关键元素长时间未出现；由 run() 关页重开后从入口重试。"""


NNC1_LOADING_TIMEOUT_MSG = "页面载入中超时，遮罩仍在"
NNC1_LOADING_RETRY_CAP = 1


class IcrisLoadingTimeoutError(IcrisFlowError):
    """储存及继续 / 继续 / 接受 之后「载入中」等到上限仍未消失。"""

    def __init__(self, message: str = NNC1_LOADING_TIMEOUT_MSG, **kwargs: Any) -> None:
        super().__init__(message or NNC1_LOADING_TIMEOUT_MSG, **kwargs)


def is_nnc1_loading_timeout(err: object) -> bool:
    if isinstance(err, IcrisLoadingTimeoutError):
        return True
    return NNC1_LOADING_TIMEOUT_MSG in str(err or "") or "载入中超时" in str(err or "")


def normalize_id_number_key(id_number: str) -> str:
    """比对用：去空格、大写、全角括号/数字折成半角。"""
    s = unicodedata.normalize("NFKC", str(id_number or ""))
    s = s.strip().upper().replace(" ", "").replace("\u3000", "")
    return s.replace("（", "(").replace("）", ")")


def is_id_already_registered_error(errs: list[str] | str | None) -> bool:
    """ICRIS s04：相同证件号已在系统登记（證件/證明、简繁）。"""
    if isinstance(errs, str):
        blob = errs
    else:
        blob = "\n".join(str(x) for x in (errs or []))
    compact = blob.replace(" ", "").replace("\u3000", "")
    return bool(
        re.search(
            r"相同的身[份分][證证][件明](?:號碼|号码)已在(?:系統|系统)中(?:登記|登记)",
            compact,
        )
    )


def id_numbers_from_job_payload(payload: dict[str, Any] | None) -> list[str]:
    if not isinstance(payload, dict):
        return []
    out: list[str] = []

    def _add(value: Any) -> None:
        text = str(value or "").strip()
        if text:
            out.append(text)

    _add(payload.get("id_number"))
    for nest in ("applicant", "identity_proof", "identity"):
        block = payload.get(nest)
        if isinstance(block, dict):
            _add(block.get("id_number"))
    directors = payload.get("directors")
    if isinstance(directors, list):
        for person in directors:
            if isinstance(person, dict):
                _add(person.get("id_number"))
    founder = payload.get("founder")
    if isinstance(founder, dict):
        _add(founder.get("id_number"))
    return out
