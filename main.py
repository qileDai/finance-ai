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
    "feishu": StepName.FEISHU_CONTACT,
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
    "feishu": "① 飞书群：/资料 发 ICRIS 填写模板，/开始注册 仅跑账号注册",
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


def cmd_wework_bot(_args: argparse.Namespace) -> None:
    """启动企业微信机器人，监听群内指令"""
    from src.wework.client import WeWorkClient, WeWorkMessage
    from src.agent.registration_agent import RegistrationAgent
    from src.workflow.steps import WorkflowContext
    from config.settings import settings

    print("=" * 60)
    print("企业微信群机器人 - 香港公司工商注册")
    print("=" * 60)

    agent = RegistrationAgent()
    client = agent.workflow.wework

    # 注册 /资料 指令：发送材料清单
    def cmd_docs(msg: WeWorkMessage) -> None:
        print(f"[指令] /资料 来自 {msg.sender_name} (群: {msg.chat_id})")
        client.send_material_checklist(msg.chat_id)
        client.send_group_text(
            msg.chat_id,
            "以上是香港公司注册所需材料清单，请准备后发送 /start 开始注册流程。",
        )

    # 注册 /start 指令：开始注册流程
    def cmd_start(msg: WeWorkMessage) -> None:
        print(f"[指令] /start 来自 {msg.sender_name} (群: {msg.chat_id})")
        client.send_group_text(
            msg.chat_id,
            "收到！正在启动注册流程...\n\n"
            "请确保已准备好材料清单中的文件，流程将依次执行：\n"
            "1. 发送材料清单\n"
            "2. 搜集客户材料\n"
            "3. 确认材料\n"
            "4. 打包材料\n"
            "5. ICRIS 账号注册\n"
            "6. 读取邮箱\n"
            "7. 登录 ICRIS 填表\n"
            "8. 通知同事",
        )
        ctx = WorkflowContext(chat_id=msg.chat_id, customer_id=msg.sender_id)
        agent.workflow.run_all(ctx)
        summary_lines = [f"  - {m}" for m in ctx.messages[-5:]]
        client.send_group_text(
            msg.chat_id,
            "注册流程已完成！\n\n最后几步:\n" + "\n".join(summary_lines),
        )

    # 注册 /help 指令
    def cmd_help(msg: WeWorkMessage) -> None:
        print(f"[指令] /help 来自 {msg.sender_name}")
        client.send_group_text(
            msg.chat_id,
            "香港公司工商注册机器人\n\n"
            "可用指令：\n"
            "/资料  — 发送香港公司注册所需材料清单\n"
            "/start — 开始注册流程\n"
            "/help  — 显示此帮助信息",
        )

    client.register_command("/资料", cmd_docs)
    client.register_command("/docs", cmd_docs)
    client.register_command("/start", cmd_start)
    client.register_command("/开始注册", cmd_start)
    client.register_command("/help", cmd_help)
    client.register_command("/帮助", cmd_help)

    print(f"[企微] 已注册指令: /资料, /start, /help")
    print(f"[企微] Mock 模式: {client._mock_mode}")
    client.start_webhook_server(port=settings.wework_webhook_port, blocking=True)


def cmd_feishu_bot(_args: argparse.Namespace) -> None:
    """启动飞书机器人：发 ICRIS 填写模板，解析回填后仅跑账号注册"""
    import logging

    from config.settings import settings
    from src.feishu.client import FeishuMessage
    from src.feishu.icris_form_parser import parse_icris_form, save_runtime_data
    from src.agent.registration_agent import RegistrationAgent
    from src.workflow.steps import WorkflowContext

    logger = logging.getLogger(__name__)

    print("=" * 60)
    print("飞书群机器人 - ICRIS 账号注册")
    print("=" * 60)

    agent = RegistrationAgent()
    client = agent.workflow.feishu

    target = client.resolve_target_chat_id()
    print(f"[飞书] Mock 模式: {client._mock_mode}")
    print(f"[飞书] 目标群: {settings.feishu_chat_name or '(未配置)'} → {target or '(未找到)'}")
    print(f"[飞书] Webhook: {'已配置' if settings.feishu_webhook_configured else '未配置'}")

    def _reply_chat(msg: FeishuMessage) -> str:
        """回消息优先走群 Webhook（自定义机器人），否则用消息所在 chat_id"""
        if settings.feishu_webhook_configured:
            return "webhook"
        return msg.chat_id

    def cmd_docs(msg: FeishuMessage) -> None:
        print(f"[指令] /资料 来自 {msg.sender_name} (群: {msg.chat_id})")
        cid = _reply_chat(msg)
        client.send_icris_register_form(cid)
        client.send_group_text(
            cid,
            "请按模板填写后，@应用机器人 发送：\n/开始注册\n并粘贴整段已填写内容。",
        )

    def cmd_start(msg: FeishuMessage) -> None:
        print(f"[指令] /开始注册 来自 {msg.sender_name} (群: {msg.chat_id})")
        cid = _reply_chat(msg)
        result = parse_icris_form(msg.content)
        if not result.ok:
            parts = []
            if result.missing:
                parts.append("缺少必填项：\n- " + "\n- ".join(result.missing))
            if result.errors:
                parts.append("校验错误：\n- " + "\n- ".join(result.errors))
            parts.append("请补全后再发送 /开始注册 + 整段模板。发送 /资料 可重新获取空白模板。")
            client.send_group_text(cid, "\n\n".join(parts))
            return

        save_runtime_data(msg.chat_id, result.data)
        client.send_group_text(
            cid,
            "资料已校验通过，正在启动 ICRIS 账号注册（仅填写，不会提交）…",
        )
        ctx = WorkflowContext(
            chat_id=msg.chat_id,
            customer_id=msg.sender_id,
            company_data=result.data,
        )
        try:
            agent.workflow.step_icris_register(ctx)
            client.send_group_text(
                cid,
                "ICRIS 账号注册表单已填写完成（dry_run，未提交）。\n"
                f"用户名: {result.data.get('icris_account', {}).get('username', '')}\n"
                f"申请人: {result.data.get('applicant', {}).get('name_en', '')}",
            )
        except Exception as e:
            logger.exception("ICRIS 注册失败")
            client.send_group_text(cid, f"注册流程异常: {e}")

    def cmd_help(msg: FeishuMessage) -> None:
        print(f"[指令] /help 来自 {msg.sender_name}")
        client.send_group_text(
            _reply_chat(msg),
            "ICRIS 账号注册机器人\n\n"
            "可用指令：\n"
            "/资料  — 发送 ICRIS 账号注册填写模板\n"
            "/开始注册 — 提交已填写模板并开始注册（仅账号注册）\n"
            "/help  — 显示此帮助\n\n"
            "用法：先 /资料 → 填好模板 → @应用机器人 /开始注册 + 粘贴内容",
        )

    client.register_command("/资料", cmd_docs)
    client.register_command("/docs", cmd_docs)
    client.register_command("/开始注册", cmd_start)
    client.register_command("/start", cmd_start)
    client.register_command("/help", cmd_help)
    client.register_command("/帮助", cmd_help)

    print("[飞书] 已注册指令: /资料, /开始注册, /help")

    # 启动前诊断：能否真正收到群消息
    try:
        import lark_oapi  # noqa: F401

        print("[飞书] lark-oapi: 已安装")
    except ImportError:
        print(
            "[飞书] 错误: 当前解释器未安装 lark-oapi，无法收消息。\n"
            "请执行: .\\.venv\\Scripts\\python.exe -m pip install lark-oapi\n"
            "然后用: .\\.venv\\Scripts\\python.exe main.py feishu-bot"
        )
        raise SystemExit(1)

    joined = client.list_joined_chats()
    print(f"[飞书] 应用机器人已加入群数: {len(joined)}")
    for ch in joined[:10]:
        print(f"  - {ch.get('name')} ({ch.get('chat_id')})")
    if not joined:
        print(
            "\n[飞书] 关键原因：开放平台应用机器人「公司注册AI自动化」还没进群！\n"
            "自定义 Webhook 机器人只能发消息，收不到 @指令。\n"
            "请在群里：添加机器人 → 搜索并添加「公司注册AI自动化」\n"
            "（对应 App ID: "
            f"{settings.feishu_app_id}）\n"
            "加群后重新运行本命令，再 @公司注册AI自动化 /资料\n"
        )
        raise SystemExit(2)

    if target:
        try:
            client.send_startup_notice(target)
        except Exception as e:
            print(f"[飞书] 启动通知发送失败: {e}")

    print("[飞书] 开始监听… 请在群里 @公司注册AI自动化 /资料")
    client.start_ws_listener(blocking=True)


def cmd_feishu_push(_args: argparse.Namespace) -> None:
    """向目标群推送 ICRIS 填写模板（Webhook 或 chat_id）"""
    from config.settings import settings
    from src.feishu.client import FeishuClient

    client = FeishuClient()
    chat_id = client.resolve_target_chat_id()
    if not chat_id:
        print(
            "未找到目标群。请确认：\n"
            "1) .env 配置 FEISHU_WEBHOOK_URL=群自定义机器人地址（推荐）\n"
            "2) 或 FEISHU_CHAT_ID=oc_xxx，并把应用机器人拉进群\n"
            "3) 飞书应用已开通 im:message / im:chat 等权限"
        )
        return
    client.send_icris_register_form(chat_id)
    via = "Webhook" if chat_id == "webhook" or settings.feishu_webhook_configured else chat_id
    print(f"已向群发送 ICRIS 填写模板（via {via}）")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="香港公司工商注册智能体",
    )
    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="运行智能体（默认命令）")
    run_parser.add_argument("--step", "-s", choices=list(STEP_CHOICES.keys()), help="仅运行指定步骤")
    run_parser.add_argument("--chat-id", default="mock_chat_001", help="企微群 ID")
    run_parser.add_argument("--full", action="store_true", help="运行完整流程")
    run_parser.set_defaults(func=cmd_run)

    steps_parser = sub.add_parser("steps", help="列出所有可用步骤")
    steps_parser.set_defaults(func=cmd_steps)

    wework_parser = sub.add_parser("wework-bot", help="启动企业微信群机器人（监听 /资料 /start 指令）")
    wework_parser.set_defaults(func=cmd_wework_bot)

    feishu_parser = sub.add_parser(
        "feishu-bot",
        help="启动飞书群机器人（/资料 发模板，/开始注册 仅跑 ICRIS 账号注册）",
    )
    feishu_parser.set_defaults(func=cmd_feishu_bot)

    feishu_push = sub.add_parser(
        "feishu-push",
        help="向配置的飞书群推送 ICRIS 填写模板（不启动监听）",
    )
    feishu_push.set_defaults(func=cmd_feishu_push)

    # 兼容直接 python main.py [--step ...] 用法
    parser.add_argument("--step", "-s", choices=list(STEP_CHOICES.keys()), dest="_step")
    parser.add_argument("--chat-id", default="mock_chat_001", dest="_chat_id")
    parser.add_argument("--full", action="store_true", dest="_full")

    args = parser.parse_args()

    if args.command == "run":
        cmd_run(args)
    elif args.command == "steps":
        cmd_steps(args)
    elif args.command == "wework-bot":
        cmd_wework_bot(args)
    elif args.command == "feishu-bot":
        cmd_feishu_bot(args)
    elif args.command == "feishu-push":
        cmd_feishu_push(args)
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
