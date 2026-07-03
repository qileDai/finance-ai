"""材料收集与打包"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from config.settings import PROJECT_ROOT

logger = logging.getLogger(__name__)

MATERIALS_DIR = PROJECT_ROOT / "data" / "materials"
OUTPUT_DIR = PROJECT_ROOT / "output"


def load_mock_data() -> dict[str, Any]:
    mock_path = PROJECT_ROOT / "data" / "mock" / "company_registration.json"
    return json.loads(mock_path.read_text(encoding="utf-8"))


def sanitize_folder_name(name: str) -> str:
    invalid = '<>:"/\\|?*'
    for ch in invalid:
        name = name.replace(ch, "_")
    return name.strip() or "unnamed_company"


def collect_materials_from_dict(data: dict[str, Any]) -> dict[str, Any]:
    """验证并整理材料字段"""
    required = ["company_name_en", "directors", "founder_members", "company_secretary"]
    missing = [k for k in required if k not in data or not data[k]]
    return {"data": data, "missing": missing, "complete": len(missing) == 0}


def package_materials(company_data: dict[str, Any], source_files: list[Path] | None = None) -> Path:
    """
    将材料打包成以公司名命名的文件夹
    包含: company_info.json + 附件副本
    """
    company_name = company_data.get("company_name_en") or company_data.get("company_name_cn", "company")
    folder_name = sanitize_folder_name(company_name)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    package_dir = OUTPUT_DIR / f"{folder_name}_{timestamp}"
    package_dir.mkdir(parents=True, exist_ok=True)

    info_path = package_dir / "company_info.json"
    info_path.write_text(json.dumps(company_data, ensure_ascii=False, indent=2), encoding="utf-8")

    attachments_dir = package_dir / "attachments"
    attachments_dir.mkdir(exist_ok=True)

    if source_files:
        for src in source_files:
            if src.exists():
                shutil.copy2(src, attachments_dir / src.name)

    # 生成材料清单摘要
    summary_lines = [
        f"# {company_name} 注册材料包",
        f"打包时间: {datetime.now().isoformat()}",
        "",
        "## 基本信息",
        f"- 英文名: {company_data.get('company_name_en', 'N/A')}",
        f"- 中文名: {company_data.get('company_name_cn', 'N/A')}",
        f"- 董事人数: {len(company_data.get('directors', []))}",
        f"- 创办成员: {len(company_data.get('founder_members', []))}",
    ]
    (package_dir / "README.md").write_text("\n".join(summary_lines), encoding="utf-8")

    logger.info("材料已打包至: %s", package_dir)
    return package_dir
