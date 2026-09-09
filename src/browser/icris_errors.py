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
    """步骤关键元素长时间未出现；由 worker 按 attempts 决定是否重跑。"""


NNC1_LOADING_TIMEOUT_MSG = "页面载入中超时，遮罩仍在"
NNC1_LOADING_RETRY_CAP = 1
# 注册切步载入超时：第 1 次失败后重跑一次（attempts < 2），不改全局 max_attempts。
REGISTER_LOADING_RETRY_CAP = NNC1_LOADING_RETRY_CAP


class IcrisLoadingTimeoutError(IcrisFlowError):
    """储存及继续 / 继续 / 接受 之后「载入中」等到上限仍未消失。"""

    def __init__(self, message: str = NNC1_LOADING_TIMEOUT_MSG, **kwargs: Any) -> None:
        super().__init__(message or NNC1_LOADING_TIMEOUT_MSG, **kwargs)


def is_nnc1_loading_timeout(err: object) -> bool:
    if isinstance(err, IcrisLoadingTimeoutError):
        return True
    return NNC1_LOADING_TIMEOUT_MSG in str(err or "") or "载入中超时" in str(err or "")


def register_loading_timeout_message(step: str) -> str:
    token = str(step or "").strip() or "页面"
    if "载入中超时" in token:
        return token
    return f"{token} 页面载入中超时，遮罩仍在"


def register_failure_should_requeue(
    err: object,
    attempts: int,
    max_attempts: int,
) -> bool:
    """注册失败是否自动重跑。载入超时只重跑一次；其它失败走 max_attempts。"""
    if getattr(err, "no_requeue", False):
        return False
    if is_nnc1_loading_timeout(err):
        return int(attempts or 0) < (1 + REGISTER_LOADING_RETRY_CAP)
    return int(attempts or 0) < int(max_attempts or 0)


def normalize_id_number_key(id_number: str) -> str:
    """比对用：去空格、大写、全角括号/数字折成半角。"""
    s = unicodedata.normalize("NFKC", str(id_number or ""))
    s = s.strip().upper().replace(" ", "").replace("\u3000", "")
    return s.replace("（", "(").replace("）", ")")


def is_icris_format_invalid_error(errs: list[str] | str | None) -> bool:
    """表单「格式不正确/不正確」。不含证件已登记、用户名占用。"""
    compact = _error_blob(errs).replace(" ", "").replace("\u3000", "")
    return "格式不正确" in compact or "格式不正確" in compact


def is_icris_username_taken_error(errs: list[str] | str | None) -> bool:
    """s02 用户名称已被使用 / 已存在。"""
    compact = _error_blob(errs).replace(" ", "").replace("\u3000", "")
    if re.search(
        r"(?:用戶名稱|用户名称).{0,16}已(?:被使用|存在|被佔用|被占用)",
        compact,
    ):
        return True
    blob = _error_blob(errs)
    return bool(
        re.search(
            r"(?:user\s*id|username).{0,24}already\s+(?:been\s+)?(?:used|taken|exist)",
            blob,
            re.I,
        )
    )


def _error_blob(errs: list[str] | str | None) -> str:
    if isinstance(errs, str):
        return errs
    return "\n".join(str(x) for x in (errs or []))


def is_id_already_registered_error(errs: list[str] | str | None) -> bool:
    """ICRIS s04：相同证件号已在系统登记（證件/證明、简繁）。"""
    compact = _error_blob(errs).replace(" ", "").replace("\u3000", "")
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
