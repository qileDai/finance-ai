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
    """浏览器注册/登录步骤检查验证码与 Playwright 依赖"""
    if step not in ("register", "login", None):
        return

    try:
        from src.browser.launcher import import_async_playwright

        import_async_playwright()
    except RuntimeError as e:
        print(f"[警告] Playwright 不可用: {e}")
    except Exception as e:
        print(f"[警告] Playwright 检查失败: {e}")

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


def cmd_wework_external_bot(_args: argparse.Namespace) -> None:
    """启动企业微信外部群机器人（客户群回调 + 存档 + AI 回复）"""
    import logging

    from config.settings import settings
    from src.storage.db import ExternalGroupStore
    from src.wework.archive_client import ArchiveClient
    from src.web.collect_server import UnifiedWebServer, CALLBACK_PATH, ADMIN_PATH, FORM_PREFIX
    from src.wework.message_router import MessageRouter

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print("=" * 60)
    print("企业微信外部群机器人 - Phase 1")
    print("=" * 60)

    mode = settings.wework_external_mode_resolved
    store = ExternalGroupStore()
    router = MessageRouter()
    archive = ArchiveClient(store=store)
    port = settings.wework_external_callback_port

    print(f"[外部群] 运行模式: {mode}")
    print(f"[外部群] 企微已配置: {settings.wework_configured}")
    print(f"[外部群] 回调已配置: {settings.wework_external_callback_configured}")
    print(f"[外部群] 存档已配置: {settings.wework_archive_configured}")
    print(f"[外部群] 回调端口: {port}")
    print(f"[外部群] 回调路径: {CALLBACK_PATH}")
    print(f"[外部群] 表单路径: {FORM_PREFIX}{{token}}")
    print(f"[外部群] 管理后台: {ADMIN_PATH}")
    print(f"[外部群] 表单公网地址: {settings.collect_form_base_url or '(使用本机 8081)'}")
    print(f"[外部群] 存档轮询间隔: {settings.wework_archive_poll_interval}s")
    print(f"[外部群] 默认群主: {settings.wework_default_group_owner_userid or '(未配置)'}")

    groups = store.list_groups()
    print(f"[外部群] 已注册群数: {len(groups)}")
    for g in groups[:10]:
        print(f"  - {g.get('name') or '(无名)'} ({g.get('roomid')}) status={g.get('status')}")

    if mode == "mock":
        print(
            "\n[外部群] 当前为 mock 模式（存档未配置或 WEWORK_EXTERNAL_MODE=mock）。\n"
            "  - 建群欢迎语：需企微回调可达，或另开终端执行 simulate\n"
            "  - 客户消息测试：python main.py wework-external-mock --roomid <群ID> --text \"问题\"\n"
            "  - 模拟建群：python main.py wework-external-mock --roomid <群ID> --create-group\n"
            "  - 模拟文件：python main.py wework-external-mock --roomid <群ID> --file path/to/id.jpg\n"
        )
    elif mode == "live":
        if not settings.wework_archive_configured:
            print("\n[外部群] 警告: live 模式但存档 Secret/私钥未配置，无法收客户群消息")
        elif not archive.sdk_available:
            print(
                "\n[外部群] 警告: 未找到 Finance SDK（vendor/wework-sdk/），"
                "请将企微存档 SDK 放到该目录并配置 WEWORK_ARCHIVE_SDK_PATH"
            )
        else:
            print("[外部群] 存档 SDK: 已找到")

    if not settings.wework_external_callback_configured:
        print(
            "\n[外部群] 提示: 回调 Token/AESKey 未配置，Webhook 无法验签。"
            "请在 .env 配置 WEWORK_EXTERNAL_CALLBACK_TOKEN 与 WEWORK_EXTERNAL_CALLBACK_AES_KEY"
        )

    archive.start_polling(router, blocking=False)
    web = UnifiedWebServer(router=router, port=port)
    print(f"\n[外部群] 开始监听… 回调 URL: https://<域名>{CALLBACK_PATH}")
    print(f"[外部群] 管理后台: http://127.0.0.1:{port}{ADMIN_PATH}")
    print("[外部群] 按 Ctrl+C 退出\n")
    try:
        web.start(blocking=True)
    finally:
        archive.close()


def _resolve_mock_from_id(router, roomid: str, from_id: str) -> str:
    """Mock 时优先使用真实外部联系人 wm ID，便于 kf 自动回复"""
    if from_id and from_id.startswith("wm"):
        return from_id
    members = router.state_machine.external.external_userids_in_group(roomid)
    if members:
        chosen = members[0]
        if from_id and from_id != "mock_external_user":
            print(f"[Mock] from-id 非 wm 开头，改用群内外部成员: {chosen}")
        else:
            print(f"[Mock] 自动选用群 {roomid} 的外部成员: {chosen}")
        return chosen
    return from_id


def _print_mock_send_context(router, roomid: str, *, from_id: str | None = None) -> None:
    from config.settings import settings

    ext = router.state_machine.external
    print(f"[Mock] 目标群 roomid={roomid}（仅该群，不波及其他群）")
    print(f"[Mock] 发送模式={settings.wework_external_send_mode_resolved}")
    print(f"[Mock] 微信客服已配置={settings.wework_kf_configured}")
    members = ext.external_userids_in_group(roomid)
    if members:
        print(f"[Mock] 群内外部成员 {len(members)} 人: {', '.join(members[:3])}{'…' if len(members) > 3 else ''}")
    plan = ext.describe_send_plan(roomid, to_external_userid=from_id if from_id and from_id.startswith("wm") else None)
    print(f"[Mock] 发送计划: {plan}")


def cmd_wework_external_mock(args: argparse.Namespace) -> None:
    """Mock 注入外部群消息或模拟建群"""
    from config.settings import settings
    from src.wework.message_router import MessageRouter

    router = MessageRouter()
    roomid = args.roomid
    if args.create_group:
        _print_mock_send_context(router, roomid)
        print(f"[Mock] 模拟建群事件 force={args.force}")
        router.simulate_group_create(roomid, force=args.force)
        mode = settings.wework_external_send_mode_resolved
        if mode == "kf":
            print("[Mock] 完成。已尝试对【当前群】外部成员 kf 自动私聊（无需群主确认）。")
        elif mode == "mass":
            print("[Mock] 完成。已创建【当前群】企业群发任务，需群主在企微确认。")
        else:
            print("[Mock] 完成。欢迎语已尝试发送到【当前群】。")
        return

    if args.file:
        from pathlib import Path

        if not Path(args.file).is_file():
            print(f"文件不存在: {args.file}")
            raise SystemExit(1)
        router.inject_mock_file(roomid, args.file, from_id=args.from_id or "mock_external_user")
        print("[Mock] 文件已注入并尝试分类入库")
        return

    if not args.text:
        print("请提供 --text、--file 或 --create-group")
        raise SystemExit(1)

    from_id = _resolve_mock_from_id(router, roomid, args.from_id or "mock_external_user")
    _print_mock_send_context(router, roomid, from_id=from_id)
    print(f"[Mock] 存档模式: {settings.wework_external_mode_resolved}")
    router.inject_mock_message(roomid, args.text, from_id=from_id)
    print("[Mock] 消息已注入；若 bot 在同进程外，请确保先启动 wework-external-bot")
    print("[Mock] 注：inject 在本进程内直接处理，无需 bot 运行")
    # inject_mock_message 已在当前进程处理
    import time
    time.sleep(6)  # 等待防抖 flush
    print("[Mock] 处理完成（含 AI 防抖等待）")


def cmd_rag_ingest(args: argparse.Namespace) -> None:
    try:
        from src.rag.pipeline import RagPipeline
    except ImportError as e:
        print(f"[RAG] 依赖缺失: {e}")
        print("[RAG] 请执行: pip install -r requirements.txt")
        sys.exit(1)
    from config.settings import PROJECT_ROOT, settings

    pipeline = RagPipeline()
    if args.file:
        path = Path(args.file)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if not path.is_file():
            print(f"[RAG] 文件不存在: {path}")
            sys.exit(1)
        changed = pipeline.ingest_file(path)
        print(f"[RAG] {'已入库' if changed else '跳过（未变更）'}: {path}")
        return

    directory = Path(settings.rag_knowledge_dir)
    if not directory.is_absolute():
        directory = PROJECT_ROOT / directory
    result = pipeline.ingest_directory(directory)
    print(f"[RAG] 入库完成: 新增/更新 {result.ingested}，跳过 {result.skipped}")
    if result.errors:
        print("[RAG] 错误:")
        for err in result.errors:
            print(f"  - {err}")
        sys.exit(1)


def cmd_rag_query(args: argparse.Namespace) -> None:
    from config.settings import settings
    from src.llm.openai_client import LLMClient
    from src.rag.prompt import format_hits_for_prompt

    query = args.query.strip()
    if not query:
        print("[RAG] 请提供查询问题")
        sys.exit(1)

    try:
        from src.rag.hybrid_retriever import HybridRetriever
    except ImportError as e:
        print(f"[RAG] 依赖缺失: {e}")
        print("[RAG] 请执行: pip install -r requirements.txt")
        sys.exit(1)

    retriever = HybridRetriever()
    hits = retriever.retrieve(query, top_k=settings.rag_top_k)
    if not hits:
        print("[RAG] 未命中任何片段")
    else:
        print(f"[RAG] 命中 {len(hits)} 条:\n")
        for i, hit in enumerate(hits, start=1):
            channels = ", ".join(f"{k}={v:.3f}" for k, v in hit.channels.items())
            print(f"--- #{i} score={hit.score:.4f} ({channels}) ---")
            print(f"来源: {hit.source_path}")
            preview = hit.text[:300] + ("…" if len(hit.text) > 300 else "")
            print(preview)
            print()

    if args.answer:
        context = format_hits_for_prompt(hits)
        llm = LLMClient()
        answer = llm.answer_material_question(query, context=context)
        print("=" * 60)
        print("LLM 回答:")
        print(answer)


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

    ext_bot = sub.add_parser(
        "wework-external-bot",
        help="启动企业微信外部群机器人（客户群欢迎语 + AI 问答 + 转人工）",
    )
    ext_bot.set_defaults(func=cmd_wework_external_bot)

    ext_mock = sub.add_parser(
        "wework-external-mock",
        help="Mock：注入客户群消息或模拟建群（开发联调）",
    )
    ext_mock.add_argument("--roomid", required=True, help="客户群 chat_id / roomid")
    ext_mock.add_argument("--text", default="", help="模拟客户发送的文本")
    ext_mock.add_argument("--file", default="", help="模拟客户上传的文件路径")
    ext_mock.add_argument("--from-id", default="mock_external_user", help="模拟外部联系人 ID")
    ext_mock.add_argument(
        "--create-group",
        action="store_true",
        help="模拟 change_external_chat create 事件（触发欢迎语）",
    )
    ext_mock.add_argument(
        "--force",
        action="store_true",
        help="建群欢迎已发送过时仍强制重发",
    )
    ext_mock.set_defaults(func=cmd_wework_external_mock)

    rag_ingest = sub.add_parser("rag-ingest", help="入库 docs/knowledge/ 知识文档到 RAG")
    rag_ingest.add_argument("--file", default="", help="仅入库指定文件")
    rag_ingest.set_defaults(func=cmd_rag_ingest)

    rag_query = sub.add_parser("rag-query", help="RAG 检索调试（可选 --answer 走 LLM）")
    rag_query.add_argument("query", help="查询问题")
    rag_query.add_argument("--answer", action="store_true", help="检索后调用 LLM 生成回答")
    rag_query.set_defaults(func=cmd_rag_query)

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
    elif args.command == "wework-external-bot":
        cmd_wework_external_bot(args)
    elif args.command == "wework-external-mock":
        cmd_wework_external_mock(args)
    elif args.command == "rag-ingest":
        cmd_rag_ingest(args)
    elif args.command == "rag-query":
        cmd_rag_query(args)
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
