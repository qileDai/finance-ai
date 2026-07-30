"""应用配置"""

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # OpenAI
    openai_api_key: str = ""
    openai_api_base: str = "https://ai-yyds.com/v1"
    openai_model: str = "gpt-4o-mini"

    # 企业微信
    wework_corp_id: str = ""
    wework_corp_secret: str = ""
    wework_agent_id: str = ""
    wework_token: str = ""
    wework_encoding_aes_key: str = ""
    wework_webhook_port: int = 8080

    # 企业微信外部群（客户群 + 会话存档）
    wework_external_callback_token: str = ""
    wework_external_callback_aes_key: str = ""
    wework_external_callback_port: int = 8081
    wework_archive_secret: str = ""
    wework_archive_private_key_path: str = ""
    wework_archive_poll_interval: int = 3
    wework_archive_sdk_path: str = ""
    wework_default_group_owner_userid: str = ""
    wework_welcome_advisor_phone: str = ""  # 建群欢迎语中的服务老师电话，空则显示【待补充】
    wework_welcome_auto_checklist: bool = True  # 建群欢迎语后自动发送注册资料清单
    wework_external_mode: str = "auto"  # auto | mock | live
    # 外部群消息发送：mass=企业群发(需群主确认) | kf=微信客服私聊(自动) | auto=优先 kf/webhook
    wework_external_send_mode: str = "auto"
    # 微信客服（kf 模式必填，Secret 在管理后台「微信客服」获取，非应用 Secret）
    wework_kf_secret: str = ""
    wework_kf_open_kfid: str = ""
    wework_kf_sync_enabled: bool = True  # 轮询 sync_msg 接收客服私聊并入智能回复
    wework_kf_poll_interval: int = 3
    # 可选：群 Webhook（仅内部群支持，外部客户群不可用）
    wework_external_group_webhook_url: str = ""

    # H5 材料收集表单
    collect_form_enabled: bool = False  # True 时才暴露 H5 链接与 /collect/form 路由
    collect_form_base_url: str = ""
    collect_form_jwt_secret: str = ""

    # 对象存储（可选，未配置则使用 data/materials/ 本地存储）
    oss_endpoint: str = ""
    oss_bucket: str = ""
    oss_access_key: str = ""
    oss_secret_key: str = ""

    # 飞书
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_chat_id: str = ""  # 目标群 chat_id（oc_xxx），优先使用
    feishu_chat_name: str = "1032044电器（深圳）有限公司"  # 按名称查找群
    # 群自定义机器人 Webhook（只负责往群里发消息；收指令仍需应用机器人）
    feishu_webhook_url: str = ""

    @property
    def feishu_webhook_configured(self) -> bool:
        return bool((self.feishu_webhook_url or "").strip())

    # 邮箱
    email_imap_host: str = "imap.example.com"
    email_imap_port: int = 993
    email_address: str = ""
    email_password: str = ""

    # 浏览器
    browser_headless: bool = False
    browser_slow_mo: int = 0
    browser_channel: str = ""  # chrome / msedge / chromium，留空则自动检测
    browser_no_proxy: bool = True  # 绕过系统代理，避免 ERR_PROXY_CONNECTION_FAILED
    browser_keep_open_seconds: int = 15
    # Chrome CDP（连接用户已打开的 Chrome，绕过 disable-devtool 检测）
    chrome_use_existing: bool = False
    chrome_cdp_url: str = "http://127.0.0.1:9222"

    # 验证码识别
    captcha_mode: str = "auto"
    captcha_manual_timeout: int = 180
    twocaptcha_api_key: str = ""
    twocaptcha_max_variants: int = 1  # 1=最快（单图）；3~5 提高准确率但更慢
    twocaptcha_timeout: int = 60  # 单次 2Captcha 任务超时（秒）
    twocaptcha_poll_interval: float = 1.0  # 轮询间隔（秒），首查不等待
    captcha_save_debug: bool = True
    # Ollama 本地视觉模型（免费，推荐 qwen2.5vl:7b）
    ollama_base_url: str = "http://localhost:11434"
    ollama_vision_model: str = ""  # 留空则不使用，如 qwen2.5vl:7b

    # 工作流
    dry_run: bool = True
    notify_colleague_open_id: str = ""

    # RAG 知识检索（SQLite FTS5 + Qdrant）
    rag_enabled: bool = True
    rag_db_path: str = "data/rag.db"
    rag_knowledge_dir: str = "docs/knowledge"
    rag_embedding_model: str = "text-embedding-3-small"
    rag_top_k: int = 8
    rag_rrf_k: int = 60
    rag_scope: str = "hk"  # hk | cn | all
    rag_primary_sources: str = "docs/knowledge/注册.md"
    rag_primary_boost: float = 1.5
    rag_exclude_patterns: str = "~$*,*.docx"
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_api_key: str = ""
    qdrant_collection: str = "finance_knowledge"

    # QA Agent Loop（检索/回答打分 + 自我纠错）
    agent_max_retries: int = 2
    agent_retrieval_threshold: float = 0.55
    agent_retrieval_llm_threshold: float = 0.45
    agent_answer_faithfulness_threshold: float = 0.7
    agent_answer_completeness_threshold: float = 0.6
    agent_answer_llm_threshold: float = 0.65
    agent_abstain_on_low_confidence: bool = True
    agent_escalate_to_human: bool = False
    agent_enable_llm_judge: bool = True
    agent_llm_judge_always: bool = False
    agent_log_runs: bool = True
    agent_silent_on_no_answer: bool = True
    agent_contextual_fallback: bool = True
    agent_context_history_limit: int = 10
    agent_abstain_message_to_customer: bool = False

    def rag_primary_source_list(self) -> list[str]:
        return [p.strip() for p in (self.rag_primary_sources or "").split(",") if p.strip()]

    def rag_exclude_pattern_list(self) -> list[str]:
        return [p.strip() for p in (self.rag_exclude_patterns or "").split(",") if p.strip()]

    @property
    def wework_configured(self) -> bool:
        return bool(self.wework_corp_id and self.wework_corp_secret and self.wework_agent_id)

    @property
    def wework_webhook_configured(self) -> bool:
        return self.wework_configured and bool(self.wework_token and self.wework_encoding_aes_key)

    @property
    def wework_external_callback_token_resolved(self) -> str:
        return (self.wework_external_callback_token or self.wework_token or "").strip()

    @property
    def wework_external_callback_aes_key_resolved(self) -> str:
        return (self.wework_external_callback_aes_key or self.wework_encoding_aes_key or "").strip()

    @property
    def wework_external_callback_configured(self) -> bool:
        return self.wework_configured and bool(
            self.wework_external_callback_token_resolved
            and self.wework_external_callback_aes_key_resolved
        )

    @property
    def wework_archive_configured(self) -> bool:
        return bool(
            self.wework_corp_id
            and self.wework_archive_secret
            and self.wework_archive_private_key_path
        )

    @property
    def wework_external_mode_resolved(self) -> str:
        mode = (self.wework_external_mode or "auto").strip().lower()
        if mode == "auto":
            return "live" if self.wework_archive_configured else "mock"
        return mode

    @property
    def wework_kf_configured(self) -> bool:
        return bool(
            self.wework_corp_id
            and (self.wework_kf_secret or "").strip()
            and (self.wework_kf_open_kfid or "").strip()
        )

    @property
    def wework_external_send_mode_resolved(self) -> str:
        mode = (self.wework_external_send_mode or "auto").strip().lower()
        if mode == "auto":
            if self.wework_kf_configured:
                return "kf"
            if (self.wework_external_group_webhook_url or "").strip():
                return "webhook"
            return "mass"
        return mode

    @property
    def oss_configured(self) -> bool:
        return bool(self.oss_endpoint and self.oss_bucket and self.oss_access_key)

    @property
    def feishu_configured(self) -> bool:
        return bool(self.feishu_app_id and self.feishu_app_secret)

    @property
    def email_configured(self) -> bool:
        return bool(self.email_address and self.email_password)


settings = Settings()
