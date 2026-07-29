"""本地文件存储（未配置 OSS 时使用）"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import BinaryIO

from config.settings import PROJECT_ROOT, settings

logger = logging.getLogger(__name__)

MATERIALS_ROOT = PROJECT_ROOT / "data" / "materials"


def room_dir(roomid: str) -> Path:
    safe = roomid.replace("/", "_").replace("\\", "_")
    d = MATERIALS_ROOT / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_bytes(roomid: str, filename: str, data: bytes) -> Path:
    dest = room_dir(roomid) / filename
    dest.write_bytes(data)
    logger.info("已保存文件 %s (%d bytes)", dest, len(data))
    return dest


def save_upload(roomid: str, filename: str, stream: BinaryIO) -> Path:
    dest = room_dir(roomid) / filename
    with open(dest, "wb") as f:
        shutil.copyfileobj(stream, f)
    return dest


def public_path(local_path: Path | str) -> str:
    """返回可写入 field 的相对/绝对路径"""
    p = Path(local_path)
    if settings.oss_configured:
        # Phase 3: OSS 上传占位，当前仍返回本地路径
        logger.debug("OSS 已配置但未实现上传 SDK，使用本地路径")
    try:
        return str(p.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(p)
