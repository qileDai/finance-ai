"""探测：拉邮箱找 ICRIS 激活信并点击。不改任务状态。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.email.icris_activate import run_icris_activation_probe
from src.storage.db import ExternalGroupStore


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="探测 ICRIS 激活邮件（不改任务状态）"
    )
    parser.add_argument("--username", required=True, help="ICRIS 账号")
    parser.add_argument("--email", required=True, help="邮箱账号")
    parser.add_argument(
        "--password",
        default="",
        help="ICRIS 密码（选填；不填则只读查任务）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    store = ExternalGroupStore()
    result = run_icris_activation_probe(
        store,
        username=args.username,
        email=args.email,
        password=args.password,
    )
    src = result.get("password_source") or "none"
    print(f"found={result.get('found')} ok={result.get('ok')} password_source={src}")
    print(result.get("detail") or "")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
