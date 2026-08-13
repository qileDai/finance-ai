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

    if step in ("register", "login") and settings.browser_headless:
        print(
            "[警告] BROWSER_HEADLESS=true：看不到浏览器窗口。"
            "本机调试请设 BROWSER_HEADLESS=false（Docker/Worker 可继续无头）"
        )

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
    roomid = getattr(args, "roomid", "") or ""

    def _apply_company_data(ctx: WorkflowContext, *, for_steps: tuple[str, ...] | None = None) -> None:
        if roomid:
            from src.materials.aggregator import load_company_data_from_roomid

            ctx.company_data = load_company_data_from_roomid(roomid)
            ctx.chat_id = roomid
            print(f"[数据] 已从群 DB 加载 roomid={roomid}")
        elif for_steps is None or (args.step and args.step in for_steps):
            from src.materials.packager import load_mock_data

            ctx.company_data = load_mock_data()

    if args.step:
        step_enum = STEP_CHOICES.get(args.step)
        if not step_enum:
            print(f"未知步骤: {args.step}")
            print(f"可用: {', '.join(STEP_CHOICES.keys())}")
            sys.exit(1)

        ctx = WorkflowContext(chat_id=roomid or args.chat_id)
        if args.step in ("register", "login", "package", "confirm", "notify", "email"):
            _apply_company_data(ctx, for_steps=("register", "login", "package", "confirm", "notify", "email"))

        ctx = agent.workflow.run_step(step_enum, ctx)
    elif args.full:
        if roomid:
            from src.materials.aggregator import load_company_data_from_roomid

            company_data = load_company_data_from_roomid(roomid)
            ctx = agent.run_full_pipeline(roomid, company_data=company_data)
        else:
            ctx = agent.run_full_pipeline(args.chat_id)
    else:
        print("\n[默认] 仅运行 ICRIS 账号注册（步骤④，Mock 数据，不提交）")
        print("使用 --full 运行完整流程，--step <name> 运行指定步骤")
        print("使用 --roomid <群ID> 从 wework_external.db 加载真实材料\n")
        ctx = WorkflowContext(chat_id=roomid or args.chat_id)
        if roomid:
            _apply_company_data(ctx)
            ctx = agent.workflow.run_step(StepName.ICRIS_REGISTER, ctx)
        else:
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


def cmd_admin(_args: argparse.Namespace) -> None:
    """启动独立管理后台（React SPA + /admin/api，与 agent 分进程）。"""
    import logging

    from config.settings import settings
    from src.storage.db import ExternalGroupStore
    from src.web.admin_server import AdminWebServer
    from src.wework.external_workflow import ExternalGroupWorkflow
    from src.wework.icris_job_worker import IcrisJobWorker

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    port = int(settings.admin_port or 8082)
    store = ExternalGroupStore()
    icris_worker = IcrisJobWorker(
        store=store,
        workflow=ExternalGroupWorkflow(store=store),
    )
    icris_worker.start(blocking=False)
    print("=" * 60)
    print("Finance AI Ops - 管理后台")
    print("=" * 60)
    print(f"[Admin] 端口: {port}")
    print(f"[Admin] URL: http://127.0.0.1:{port}/admin")
    print("[Admin] 打开 /admin 登录页（ADMIN_USERNAME / ADMIN_PASSWORD）；与 wework-external-bot 共用 SQLite")
    print(
        f"[Admin] ICRIS Worker: "
        f"{'已启动' if settings.icris_worker_enabled else '未启用（ICRIS_WORKER_ENABLED=false）'}"
    )
    print("[Admin] Agent 请另开终端: python main.py wework-external-bot")
    print("[Admin] 按 Ctrl+C 退出\n")
    try:
        AdminWebServer(port=port, store=store, icris_worker=icris_worker).start(
            blocking=True
        )
    finally:
        icris_worker.stop()



def cmd_wework_external_bot(_args: argparse.Namespace) -> None:
    """启动企业微信外部群机器人（客户群回调 + 存档 + AI 回复）"""
    import logging

    from config.settings import settings
    from src.storage.db import ExternalGroupStore
    from src.wework.archive_client import ArchiveClient
    from src.web.collect_server import (
        UnifiedWebServer,
        CALLBACK_PATH,
        WEBHOOK_PATH,
        FORM_PREFIX,
    )
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
    print(f"[外部群] 双通道: {settings.wework_channel_resolved} "
          f"(群={'开' if settings.wework_channel_group_enabled else '关'}, "
          f"客服={'开' if settings.wework_channel_kf_enabled else '关'})")
    print(f"[外部群] 企微已配置: {settings.wework_configured}")
    print(f"[外部群] 回调已配置: {settings.wework_external_callback_configured}")
    print(f"[外部群] 存档已配置: {settings.wework_archive_configured}")
    print(f"[外部群] 回调端口: {port}")
    print(f"[外部群] 回调路径: {WEBHOOK_PATH}（公网优先）| {CALLBACK_PATH}（兼容）")
    if settings.collect_form_enabled:
        print(f"[外部群] 材料收集: H5 在线表单")
        print(f"[外部群] 表单路径: {FORM_PREFIX}{{token}}")
        print(f"[外部群] 表单公网地址: {settings.collect_form_base_url or '(使用本机 8081)'}")
    else:
        print(f"[外部群] 材料收集: 群内粘贴（/填表 发模板，H5 已关闭）")
    print(f"[外部群] 管理后台: 请另启 python main.py admin （默认端口 {settings.admin_port}）")
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

    if settings.wework_channel_group_enabled:
        archive.start_polling(router, blocking=False)
    else:
        print("[外部群] 群通道已关闭（WEWORK_CHANNEL 不含 group），存档 worker 未启动")

    from src.wework.kf_worker import KfSyncWorker
    from src.wework.icris_job_worker import IcrisJobWorker

    kf_worker = KfSyncWorker(
        store=store,
        external=router.state_machine.external,
        state_machine=router.state_machine,
    )
    if settings.wework_channel_kf_enabled and settings.wework_kf_configured:
        mode = settings.wework_kf_mode_resolved
        if settings.wework_kf_poll_enabled:
            print(
                f"[外部群] kf 轮询: 已启用（模式={mode}，间隔 {settings.wework_kf_poll_interval}s，"
                f"账号 {len(settings.wework_kf_open_kfid_list)} 个）"
            )
            kf_worker.start_polling(blocking=False)
        else:
            print(f"[外部群] kf 轮询: 未启用（WEWORK_KF_MODE={mode}）")
            # push-only：仍需定时恢复崩溃后未处理的 inbox
            kf_worker.start_inbox_recover()
            print("[外部群] kf inbox 恢复扫描: 已启用")
        if settings.wework_kf_push_enabled:
            if settings.wework_external_callback_configured:
                print(
                    "[外部群] kf 推送: 已启用（回调 URL 收到 kf_msg_or_event 后触发 sync_msg）"
                )
            else:
                print(
                    "[外部群] kf 推送: 需配置 WEWORK_EXTERNAL_CALLBACK_TOKEN/AESKey "
                    "并在微信客服后台填写回调 URL"
                )
    elif not settings.wework_channel_kf_enabled:
        print("[外部群] kf 通道已关闭（WEWORK_CHANNEL 不含 kf）")
    else:
        print("[外部群] kf 未启用（需 WEWORK_KF_* 配置）")

    icris_worker = IcrisJobWorker(
        store=store,
        workflow=router.state_machine.ext_workflow,
    )
    if settings.icris_worker_enabled:
        print(
            f"[外部群] ICRIS 队列 Worker: 已启用（poll={settings.icris_worker_poll_seconds}s, "
            f"max_attempts={settings.icris_job_max_attempts}, "
            f"dry_run={settings.dry_run}, allow_submit={settings.icris_allow_submit}）"
        )
        icris_worker.start(blocking=False)
    else:
        print("[外部群] ICRIS 队列 Worker: 已关闭（ICRIS_WORKER_ENABLED=false）")

    if settings.wework_welcome_auto_checklist:
        print("[外部群] 建群欢迎后自动发清单: 已启用")
    else:
        print("[外部群] 建群欢迎后自动发清单: 已关闭（客户需发 /资料）")

    web = UnifiedWebServer(
        router=router, port=port, kf_worker=kf_worker, icris_worker=icris_worker
    )
    print(f"\n[外部群] 开始监听… 公网回调: http://szyingtai.cn{WEBHOOK_PATH}")
    print(f"[外部群] 本机回调: http://127.0.0.1:{port}{WEBHOOK_PATH} 或 {CALLBACK_PATH}")
    print(f"[外部群] 管理后台: python main.py admin → http://127.0.0.1:{settings.admin_port}/admin")
    print("[外部群] 按 Ctrl+C 退出\n")
    try:
        web.start(blocking=True)
    finally:
        if settings.wework_channel_group_enabled:
            archive.close()
        kf_worker.stop_polling()
        icris_worker.stop()


def cmd_wework_kf_mock(args: argparse.Namespace) -> None:
    """Mock 注入微信客服私聊消息或模拟首次欢迎 / 回调 sync"""
    from config.settings import settings
    from src.wework.kf_session import build_kf_roomid
    from src.wework.message_router import MessageRouter

    router = MessageRouter()
    from_id = args.from_id or "wmMockKfUser001"
    if not from_id.startswith("wm"):
        from_id = f"wm{from_id}"

    open_kfid = (
        getattr(args, "open_kfid", None)
        or settings.wework_kf_default_open_kfid
        or "wkMockKf"
    )

    if not settings.wework_kf_configured:
        router.state_machine.external._mock_mode = True

    print(f"[Mock-KF] open_kfid={open_kfid}")
    print(f"[Mock-KF] 目标客户 external_userid={from_id}")
    print(f"[Mock-KF] 微信客服已配置={settings.wework_kf_configured}")
    print(f"[Mock-KF] 模式={settings.wework_kf_mode_resolved}")
    print(f"[Mock-KF] 发送模式={settings.wework_external_send_mode_resolved}")

    if getattr(args, "simulate_callback", False):
        print(f"[Mock-KF] 模拟 kf_msg_or_event 回调 → sync_for_account")
        router.simulate_kf_callback(open_kfid, token=getattr(args, "token", "") or "mock_token")
        print("[Mock-KF] 完成")
        return

    if args.first_contact:
        print("[Mock-KF] 模拟首次私聊欢迎 + 清单")
        router.simulate_kf_first_contact(from_id, open_kfid=open_kfid)
        print("[Mock-KF] 完成")
        return

    if not args.text:
        print("请提供 --text、--first-contact 或 --simulate-callback")
        raise SystemExit(1)

    router.inject_kf_message(from_id, args.text, open_kfid=open_kfid)
    import time
    time.sleep(6)
    print("[Mock-KF] 处理完成（含 AI 防抖等待）")
    roomid = build_kf_roomid(open_kfid, from_id)
    msgs = router.state_machine.external.get_mock_messages(roomid)
    if msgs:
        print(f"[Mock-KF] Mock 出站 {len(msgs)} 条:")
        for m in msgs[-3:]:
            print(f"  → {m.get('content', '')[:120]}")


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
        detail = pipeline.ingest_file_detail(path, force=getattr(args, "force", False))
        if detail.changed:
            print(f"[RAG] 已入库: {detail.source_path}")
            print(f"  chunks: {detail.chunk_count}, 字符: {detail.char_total}")
            if detail.region_stats:
                print(f"  region: {detail.region_stats}")
            if getattr(args, "verbose", False) and detail.preview:
                print(f"  预览: {detail.preview}")
        else:
            print(f"[RAG] 跳过（未变更或已排除）: {detail.source_path}")
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


def cmd_rag_status(_args: argparse.Namespace) -> None:
    try:
        from src.rag.sqlite_index import RagSqliteIndex
    except ImportError as e:
        print(f"[RAG] 依赖缺失: {e}")
        sys.exit(1)
    from config.settings import settings

    idx = RagSqliteIndex()
    docs = idx.document_chunk_stats()
    regions = idx.region_stats()
    print("[RAG] 知识库状态")
    print(f"  DB: {idx.db_path}")
    print(f"  scope 默认: {settings.rag_scope}")
    print(f"  主文档: {settings.rag_primary_sources}")
    print(f"  top_k: {settings.rag_top_k}")
    if not docs:
        print("  （尚无入库文档，请执行 rag-ingest）")
        return
    print("\n  文档:")
    for row in docs:
        print(
            f"    - {row['source_path']} | chunks={row['chunk_count']} "
            f"| chars={row['char_total'] or 0} | {row['ingested_at']}"
        )
    if regions:
        print("\n  region 分布:")
        for region, count in sorted(regions.items()):
            print(f"    - {region}: {count}")


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

    scope = getattr(args, "scope", "") or settings.rag_scope
    retriever = HybridRetriever()
    hits = retriever.retrieve(query, top_k=settings.rag_top_k, scope=scope)
    if not hits:
        print(f"[RAG] 未命中任何片段 (scope={scope})")
    else:
        print(f"[RAG] 命中 {len(hits)} 条 (scope={scope}):\n")
        show_full = getattr(args, "full", False)
        preview_limit = 600
        for i, hit in enumerate(hits, start=1):
            channels = ", ".join(f"{k}={v:.3f}" for k, v in hit.channels.items())
            region = f" region={hit.region}" if hit.region else ""
            step = f" step={hit.step_title[:40]}" if hit.step_title else ""
            kind = f" kind={hit.chunk_kind}" if hit.chunk_kind else ""
            print(
                f"--- #{i} score={hit.score:.4f} len={len(hit.text)} chars"
                f" ({channels}){region}{step}{kind} ---"
            )
            print(f"来源: {hit.source_path}")
            if show_full:
                print(hit.text)
            else:
                preview = hit.text[:preview_limit]
                if len(hit.text) > preview_limit:
                    preview += "…"
                print(preview)
            print()

    if args.answer:
        context = format_hits_for_prompt(hits)
        llm = LLMClient()
        answer = llm.answer_material_question(query, context=context)
        print("=" * 60)
        print("LLM 回答:")
        print(answer)


def cmd_agent_query(args: argparse.Namespace) -> None:
    from config.settings import settings
    from src.agent.orchestrator import TaskOrchestrator
    from src.agent.models import AgentContext

    query = args.query.strip()
    if not query:
        print("[Agent] 请提供查询问题")
        sys.exit(1)

    scope = getattr(args, "scope", "") or settings.rag_scope
    result = TaskOrchestrator().run_qa(
        AgentContext(question=query, scope=scope),
    )

    print(f"[Agent] run_id={result.run_id}")
    print(f"  action={result.action.value} mode={result.answer_mode.value} confidence={result.confidence}")
    if result.silent_reason:
        print(f"  silent_reason={result.silent_reason}")
    print(f"  retrieval_score={result.retrieval_score} answer_score={result.answer_score}")
    print(f"  retries={result.retries} citations={result.citations}")
    print()

    for step in result.trace:
        print(f"--- {step.step} attempt={step.attempt} ---")
        for k, v in step.data.items():
            print(f"  {k}: {v}")
        print()

    if result.hits:
        print(f"[Agent] 检索命中 {len(result.hits)} 条:")
        for i, hit in enumerate(result.hits[:5], start=1):
            print(f"  #{i} score={hit.score:.4f} kind={hit.chunk_kind} step={hit.step_title[:40]}")
        print()

    print("=" * 60)
    if result.action.value == "silent":
        print("最终回答: （静默，不发送客户）")
    else:
        print("最终回答:")
        print(result.answer)


def cmd_agent_eval(args: argparse.Namespace) -> None:
    import re

    from config.settings import settings
    from src.agent.models import AnswerMode
    from src.agent.orchestrator import TaskOrchestrator
    from src.agent.models import AgentContext
    from src.rag.hybrid_retriever import HybridRetriever, is_primary_source

    scope = getattr(args, "scope", "") or "hk"
    no_llm = getattr(args, "no_llm", False)
    if no_llm:
        settings.agent_enable_llm_judge = False

    GOLDEN = [
        ("进群怎么打招呼", "hk"),
        ("香港注册需要什么资料", "hk"),
        ("开户面签要注意什么", "hk"),
        ("香港开户需要多久", "hk"),
    ]
    CAUTION_MARKERS = ("面签注意事项", "被制裁国家", "仅需要面签人员", "不要拍照")
    DURATION_QUERY = "香港开户需要多久"
    DURATION_ANSWER_MARKERS = ("3-4", "周", "审核")
    NUMBERED_RE = re.compile(r"[1-9][、.)．]")

    retriever = HybridRetriever()
    orchestrator = TaskOrchestrator()
    failed = 0

    print(f"[Agent-Eval] scope={scope} llm_judge={settings.agent_enable_llm_judge}\n")

    print("--- 检索 golden ---")
    for query, expect_region in GOLDEN:
        hits = retriever.retrieve(query, scope=scope)
        if not hits:
            print(f"FAIL  无命中: {query}")
            failed += 1
            continue
        top = hits[0]
        ok = (expect_region != "hk" or top.region != "cn") and is_primary_source(top.source_path)
        print(f"{'OK' if ok else 'WARN'}  {query} → step={top.step_title[:40]} kind={top.chunk_kind}")
        if not ok:
            failed += 1

    print("\n--- 端到端 QA golden（知识库模式）---")
    caution_query = "开户面签要注意什么"
    result = orchestrator.run_qa(AgentContext(question=caution_query, scope=scope))
    merged_hits = "\n".join(h.text for h in result.hits[:3])
    missing = [m for m in CAUTION_MARKERS if m not in merged_hits and m not in result.answer]
    numbered = len(NUMBERED_RE.findall(result.answer))

    if result.answer_mode != AnswerMode.KNOWLEDGE:
        print(f"WARN  {caution_query} → mode={result.answer_mode.value}（期望 knowledge）")
        failed += 1
    elif result.action.value != "reply":
        print(f"WARN  {caution_query} → action={result.action.value}")
        failed += 1
    elif missing:
        print(f"FAIL  {caution_query} → 缺少要点: {missing}")
        failed += 1
    elif numbered < 3 and result.answer_score < 0.6:
        print(f"WARN  {caution_query} → 编号要点不足 numbered={numbered}")
        failed += 1
    else:
        print(
            f"OK    {caution_query} → mode={result.answer_mode.value} "
            f"confidence={result.confidence} numbered={numbered}"
        )

    print("\n--- 端到端 QA golden（开户时效）---")
    duration_result = orchestrator.run_qa(
        AgentContext(question=DURATION_QUERY, scope=scope),
    )
    duration_ok = (
        duration_result.action.value == "reply"
        and duration_result.answer.strip()
        and any(m in duration_result.answer for m in DURATION_ANSWER_MARKERS)
    )
    if duration_ok:
        print(
            f"OK    {DURATION_QUERY} → mode={duration_result.answer_mode.value} "
            f"action={duration_result.action.value} "
            f"answer={duration_result.answer[:60]}..."
        )
    else:
        print(
            f"FAIL  {DURATION_QUERY} → mode={duration_result.answer_mode.value} "
            f"action={duration_result.action.value} "
            f"silent={duration_result.silent_reason[:40] if duration_result.silent_reason else ''}"
        )
        failed += 1

    print("\n--- 三级策略：无关问题应静默 ---")
    off_topic = "帮我订一张明天去北京的机票"
    off_result = orchestrator.run_qa(AgentContext(question=off_topic, scope=scope))
    if off_result.action.value == "silent" and not off_result.answer.strip():
        print(f"OK    {off_topic} → silent reason={off_result.silent_reason[:40]}")
    else:
        print(f"WARN  {off_topic} → action={off_result.action.value} answer_len={len(off_result.answer)}")
        failed += 1

    print(f"\n--- bad cases (confidence < 0.5) ---")
    try:
        from src.storage.db import ExternalGroupStore

        bad = ExternalGroupStore().list_low_confidence_runs(limit=5)
        if bad:
            for row in bad:
                print(f"  {row.get('question', '')[:40]} conf={row.get('confidence')} action={row.get('action')}")
        else:
            print("  （暂无 agent_runs 低分记录）")
    except Exception as e:
        print(f"  跳过 bad case 查询: {e}")

    print(f"\n[Agent-Eval] 完成，异常项: {failed}")
    if failed:
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="香港公司工商注册智能体",
    )
    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="运行智能体（默认命令）")
    run_parser.add_argument("--step", "-s", choices=list(STEP_CHOICES.keys()), help="仅运行指定步骤")
    run_parser.add_argument("--chat-id", default="mock_chat_001", help="企微群 ID")
    run_parser.add_argument(
        "--roomid",
        default="",
        help="外部客户群 roomid；指定时从 wework_external.db 加载真实材料（用于 register/package 等）",
    )
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

    admin_parser = sub.add_parser(
        "admin",
        help="启动独立管理后台（React SPA，默认端口 ADMIN_PORT=8082）",
    )
    admin_parser.set_defaults(func=cmd_admin)

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

    kf_mock = sub.add_parser(
        "wework-kf-mock",
        help="Mock：注入微信客服私聊消息或模拟首次欢迎",
    )
    kf_mock.add_argument(
        "--from-id",
        default="wmMockKfUser001",
        help="外部联系人 wm ID",
    )
    kf_mock.add_argument(
        "--open-kfid",
        default="",
        help="客服账号 open_kfid（默认取配置首个账号）",
    )
    kf_mock.add_argument("--text", default="", help="模拟客户在客服会话发送的文本")
    kf_mock.add_argument(
        "--first-contact",
        action="store_true",
        help="模拟首次进入客服（欢迎语+清单）",
    )
    kf_mock.add_argument(
        "--simulate-callback",
        action="store_true",
        help="模拟 kf_msg_or_event 回调并触发 sync_for_account",
    )
    kf_mock.add_argument(
        "--token",
        default="",
        help="与 --simulate-callback 联用的 mock Token",
    )
    kf_mock.set_defaults(func=cmd_wework_kf_mock)

    rag_ingest = sub.add_parser("rag-ingest", help="入库 docs/knowledge/ 知识文档到 RAG")
    rag_ingest.add_argument("--file", default="", help="仅入库指定文件")
    rag_ingest.add_argument("--verbose", action="store_true", help="输出首块预览")
    rag_ingest.add_argument("--force", action="store_true", help="强制重新入库（忽略 content hash）")
    rag_ingest.set_defaults(func=cmd_rag_ingest)

    rag_status = sub.add_parser("rag-status", help="查看 RAG 知识库入库状态")
    rag_status.set_defaults(func=cmd_rag_status)

    rag_query = sub.add_parser("rag-query", help="RAG 检索调试（可选 --answer 走 LLM）")
    rag_query.add_argument("query", help="查询问题")
    rag_query.add_argument("--answer", action="store_true", help="检索后调用 LLM 生成回答")
    rag_query.add_argument("--full", action="store_true", help="打印完整 chunk 文本")
    rag_query.add_argument(
        "--scope",
        choices=["hk", "cn", "all"],
        default="",
        help="检索范围（默认 RAG_SCOPE）",
    )
    rag_query.set_defaults(func=cmd_rag_query)

    agent_query = sub.add_parser("agent-query", help="QA Agent Loop 调试（检索+打分+纠错）")
    agent_query.add_argument("query", help="查询问题")
    agent_query.add_argument(
        "--scope",
        choices=["hk", "cn", "all"],
        default="",
        help="检索范围（默认 RAG_SCOPE）",
    )
    agent_query.set_defaults(func=cmd_agent_query)

    agent_eval = sub.add_parser("agent-eval", help="Agent 端到端 golden 回归")
    agent_eval.add_argument(
        "--scope",
        choices=["hk", "cn", "all"],
        default="",
        help="检索范围（默认 RAG_SCOPE）",
    )
    agent_eval.add_argument(
        "--no-llm",
        action="store_true",
        help="禁用 LLM judge，仅用规则分",
    )
    agent_eval.set_defaults(func=cmd_agent_eval)

    # 兼容直接 python main.py [--step ...] 用法
    parser.add_argument("--step", "-s", choices=list(STEP_CHOICES.keys()), dest="_step")
    parser.add_argument("--chat-id", default="mock_chat_001", dest="_chat_id")
    parser.add_argument("--roomid", default="", dest="_roomid", help="外部群 roomid，加载 DB 材料")
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
    elif args.command == "admin":
        cmd_admin(args)
    elif args.command == "wework-external-mock":
        cmd_wework_external_mock(args)
    elif args.command == "wework-kf-mock":
        cmd_wework_kf_mock(args)
    elif args.command == "rag-ingest":
        cmd_rag_ingest(args)
    elif args.command == "rag-status":
        cmd_rag_status(args)
    elif args.command == "rag-query":
        cmd_rag_query(args)
    elif args.command == "agent-query":
        cmd_agent_query(args)
    elif args.command == "agent-eval":
        cmd_agent_eval(args)
    elif args._step or args._full or len(sys.argv) == 1:
        # 默认 run
        run_args = argparse.Namespace(
            step=args._step,
            chat_id=args._chat_id,
            roomid=getattr(args, "_roomid", "") or "",
            full=args._full,
        )
        cmd_run(run_args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
