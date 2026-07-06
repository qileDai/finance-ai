#!/usr/bin/env python3
"""香港公司工商注册智能体 CLI"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Windows 控制台 UTF-8 输出
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.registration_agent import RegistrationAgent
from src.workflow.steps import StepName, WorkflowContext

STEP_CHOICES = {
    "wework": StepName.WEWORK_CONTACT,
    "collect": StepName.COLLECT_MATERIALS,
    "confirm": StepName.CONFIRM_MATERIALS,
    "package": StepName.PACKAGE,
    "register": StepName.ICRIS_REGISTER,
    "email": StepName.READ_EMAIL,
    "login": StepName.ICRIS_LOGIN,
    "notify": StepName.NOTIFY,
}

STEP_DESCRIPTIONS = {
    "wework": "① 企微群对接客户，发送材料清单，回答材料问题",
    "collect": "② 搜集客户材料",
    "confirm": "② 和客户确认材料",
    "package": "③ 打包材料文件夹",
    "register": "④ ICRIS 账号注册（浏览器填写，不提交）",
    "email": "⑤ 读取邮箱获取 ICRIS 账号",
    "login": "⑥ 登录 ICRIS 填写注册材料",
    "notify": "⑦ 核对材料，提醒同事后续操作",
}


def print_result(ctx: WorkflowContext) -> None:
    print("\n" + "=" * 60)
    print("执行日志")
    print("=" * 60)
    for i, msg in enumerate(ctx.messages, 1):
        print(f"  {i:>3}. {msg}")
    print("=" * 60)


def cmd_steps(_args: argparse.Namespace) -> None:
    print("\n可用工作流步骤:\n")
    for name, desc in STEP_DESCRIPTIONS.items():
        print(f"  {name:10s}  {desc}")
    print()


def check_captcha_deps(step: str | None) -> None:
    """浏览器注册/登录步骤检查验证码依赖"""
    if step not in ("register", "login", None):
        return

    from config.settings import settings

    mode = (settings.captcha_mode or "auto").lower()
    if mode == "manual":
        print("[提示] 验证码模式: manual — 将在浏览器中手动输入")
        return
    if mode == "2captcha":
        if settings.twocaptcha_api_key:
            print(
                f"[提示] 验证码模式: 2captcha — 仅 2Captcha 多帧识别"
                f"（{settings.twocaptcha_max_variants} 变体并行）"
            )
            try:
                import PIL  # noqa: F401
            except ImportError:
                print("[警告] 未安装 Pillow，请执行: pip install Pillow")
        else:
            print("[警告] CAPTCHA_MODE=2captcha 但未配置 TWOCAPTCHA_API_KEY")
        return
    if mode == "auto":
        if settings.twocaptcha_api_key:
            print("[提示] 验证码: 优先 2Captcha 多帧识别，失败后 OCR/LLM 回退")
            try:
                import PIL  # noqa: F401
            except ImportError:
                print("[警告] 未安装 Pillow，2Captcha 无法工作: pip install Pillow")
        else:
            print("[提示] 验证码: 本地 OCR / 手动输入（未配置 TWOCAPTCHA_API_KEY）")
        return
    if mode == "ollama":
        if settings.ollama_vision_model:
            print(f"[提示] 验证码模式: ollama — 使用本地模型 {settings.ollama_vision_model}")
        else:
            print("[警告] CAPTCHA_MODE=ollama 但未配置 OLLAMA_VISION_MODEL")
        return
    if mode == "audio":
        print("[提示] 验证码模式: audio — 读出验证码 + Whisper 粤语识别")
        return
    if mode == "ocr":
        print("[提示] 验证码模式: ocr — 本地 ddddocr")
        try:
            import ddddocr  # noqa: F401
        except ImportError:
            print("[警告] 未安装 ddddocr")
        return


def cmd_run(args: argparse.Namespace) -> None:
    print("=" * 60)
    print("香港公司工商注册智能体")
    print("企微对接 → 材料收集 → 打包 → ICRIS注册 → 邮箱 → 登录填表 → 通知")
    print("=" * 60)

    check_captcha_deps(args.step if args.step else ("register" if not args.full else None))

    agent = RegistrationAgent()

    if args.step:
        step_enum = STEP_CHOICES.get(args.step)
        if not step_enum:
            print(f"未知步骤: {args.step}")
            print(f"可用: {', '.join(STEP_CHOICES.keys())}")
            sys.exit(1)

        ctx = WorkflowContext(chat_id=args.chat_id)
        if args.step in ("register", "login", "package", "confirm", "notify", "email"):
            from src.materials.packager import load_mock_data
            ctx.company_data = load_mock_data()

        ctx = agent.workflow.run_step(step_enum, ctx)
    elif args.full:
        ctx = agent.run_full_pipeline(args.chat_id)
    else:
        print("\n[默认] 仅运行 ICRIS 账号注册（步骤④，Mock 数据，不提交）")
        print("使用 --full 运行完整流程，--step <name> 运行指定步骤\n")
        ctx = agent.run_registration_only()

    print_result(ctx)
    print("\n[OK] 执行完成")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="香港公司工商注册智能体",
    )
    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="运行智能体（默认命令）")
    run_parser.add_argument("--step", "-s", choices=list(STEP_CHOICES.keys()), help="仅运行指定步骤")
    run_parser.add_argument("--chat-id", default="mock_chat_001", help="企微群 ID")
    run_parser.add_argument("--full", action="store_true", help="运行完整 7 步流程")
    run_parser.set_defaults(func=cmd_run)

    steps_parser = sub.add_parser("steps", help="列出所有可用步骤")
    steps_parser.set_defaults(func=cmd_steps)

    # 兼容直接 python main.py [--step ...] 用法
    parser.add_argument("--step", "-s", choices=list(STEP_CHOICES.keys()), dest="_step")
    parser.add_argument("--chat-id", default="mock_chat_001", dest="_chat_id")
    parser.add_argument("--full", action="store_true", dest="_full")

    args = parser.parse_args()

    if args.command == "run":
        cmd_run(args)
    elif args.command == "steps":
        cmd_steps(args)
    elif args._step or args._full or len(sys.argv) == 1:
        # 默认 run
        run_args = argparse.Namespace(
            step=args._step,
            chat_id=args._chat_id,
            full=args._full,
        )
        cmd_run(run_args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
