"""本地文件存储（未配置 OSS 时使用）"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import BinaryIO

from config.settings import PROJECT_ROOT, settings

logger = logging.getLogger(__name__)

# Windows 路径分量非法字符 + 控制字符
_INVALID_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def materials_root() -> Path:
    """材料根目录：MATERIALS_DIR 相对项目根，或绝对路径（生产）"""
    raw = (settings.materials_dir or "data/materials").strip()
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p.resolve()


def safe_dirname(name: str) -> str:
    """清洗为可在 Windows/Unix 上建目录的安全名"""
    safe = _INVALID_PATH_CHARS.sub("_", (name or "").strip())
    safe = safe.strip(" .")
    # 压缩连续下划线
    safe = re.sub(r"_+", "_", safe)
    return safe or "room_unknown"


def safe_room_dirname(roomid: str) -> str:
    """将 roomid 转为可在 Windows/Unix 上建目录的安全名（kf:a:b → kf_a_b）"""
    return safe_dirname(roomid)


def company_dir_name(company_name_cn: str = "", company_name_en: str = "") -> str:
    """优先中文名，其次英文名；皆空则返回空串"""
    label = (company_name_cn or "").strip() or (company_name_en or "").strip()
    if not label:
        return ""
    name = safe_dirname(label)
    return "" if name == "room_unknown" else name


def folder_dirname(roomid: str, folder_label: str = "") -> str:
    """有公司名标签用公司名，否则用 roomid 安全名"""
    if (folder_label or "").strip():
        name = safe_dirname(folder_label)
        if name and name != "room_unknown":
            return name
    return safe_room_dirname(roomid)


def room_dir(roomid: str, *, folder_label: str = "") -> Path:
    d = materials_root() / folder_dirname(roomid, folder_label)
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_bytes(
    roomid: str,
    filename: str,
    data: bytes,
    *,
    folder_label: str = "",
) -> Path:
    dest = room_dir(roomid, folder_label=folder_label) / filename
    dest.write_bytes(data)
    logger.info("已保存文件 %s (%d bytes)", dest, len(data))
    return dest


def save_upload(
    roomid: str,
    filename: str,
    stream: BinaryIO,
    *,
    folder_label: str = "",
) -> Path:
    dest = room_dir(roomid, folder_label=folder_label) / filename
    with open(dest, "wb") as f:
        shutil.copyfileobj(stream, f)
    return dest


def ensure_company_folder(
    roomid: str,
    company_name_cn: str = "",
    company_name_en: str = "",
) -> tuple[Path | None, str, str]:
    """将 roomid 目录对齐到公司名目录。

    Returns:
        (新目录 Path 或 None, 旧目录名, 新目录名)
        无公司名时返回 (None, "", "")
    """
    target = company_dir_name(company_name_cn, company_name_en)
    if not target:
        return None, "", ""

    root = materials_root()
    old_name = safe_room_dirname(roomid)
    old = root / old_name
    new = root / target

    if old_name == target:
        new.mkdir(parents=True, exist_ok=True)
        return new, old_name, target

    if not old.is_dir():
        new.mkdir(parents=True, exist_ok=True)
        logger.info("材料目录就绪（无旧目录）: %s", new)
        return new, old_name, target

    try:
        if old.resolve() == new.resolve():
            return new, old_name, target
    except OSError:
        pass

    if not new.exists():
        old.rename(new)
        logger.info("材料目录已重命名 %s → %s", old_name, target)
        return new, old_name, target

    # 目标已存在：合并文件
    new.mkdir(parents=True, exist_ok=True)
    for item in old.iterdir():
        dest = new / item.name
        if dest.exists():
            continue
        shutil.move(str(item), str(dest))
    try:
        if not any(old.iterdir()):
            old.rmdir()
    except OSError:
        logger.debug("旧材料目录未清空，保留: %s", old)
    logger.info("材料目录已合并 %s → %s", old_name, target)
    return new, old_name, target


def public_path(local_path: Path | str) -> str:
    """返回可写入 field 的相对/绝对路径"""
    p = Path(local_path)
    if settings.oss_configured:
        logger.debug("OSS 已配置但未实现上传 SDK，使用本地路径")
    try:
        return str(p.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(p)
