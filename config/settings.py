"""应用配置"""

import json
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class KfAccountConfig(BaseModel):
    open_kfid: str
    name: str = ""
    label: str = ""

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
    openai_vision_model: str = ""  # 空则回退 openai_model；证件看图识别用

    # 管理后台登录（空密码则无法登录 /admin）
    admin_username: str = Field(default="admin", validation_alias="ADMIN_USERNAME")
    admin_password: str = Field(default="", validation_alias="ADMIN_PASSWORD")
    # 独立管理后台端口（python main.py admin）；与 bot 回调端口分离
    admin_port: int = Field(default=8082, validation_alias="ADMIN_PORT")
    # Cookie 会话：可选独立密钥；空则由 ADMIN_PASSWORD 派生
    admin_session_secret: str = Field(default="", validation_alias="ADMIN_SESSION_SECRET")
    admin_session_hours: float = Field(default=12.0, validation_alias="ADMIN_SESSION_HOURS")

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
    # 双通道：group=仅外部群 | kf=仅微信客服 | both=两者并行（默认）
    wework_channel: str = "both"
    # 微信客服（kf 模式必填，Secret 在管理后台「微信客服」获取，非应用 Secret）
    wework_kf_secret: str = ""
    wework_kf_open_kfid: str = ""  # 单账号兼容；多账号请用 WEWORK_KF_ACCOUNTS
    wework_kf_accounts_json: str = Field(default="", validation_alias="WEWORK_KF_ACCOUNTS")
    # push=仅回调 | poll=仅轮询 | both=回调为主+轮询兜底（推荐生产）
    wework_kf_mode: str = "both"
    wework_kf_sync_enabled: bool = True  # poll/both 模式下启用 sync_msg 轮询
    wework_kf_poll_interval: int = 120
    # 可选：群 Webhook（仅内部群支持，外部客户群不可用）
    wework_external_group_webhook_url: str = ""
    # 资料意图分流：规则不确定时是否单次 LLM 分类（失败回退 qa）
    wework_intent_llm_fallback: bool = True
    # 意图小模型（空则回退 openai_model）；仅歧义分类，不生成对客正文
    wework_intent_model: str = Field(default="", validation_alias="WEWORK_INTENT_MODEL")
    wework_intent_timeout_seconds: float = Field(
        default=8.0, validation_alias="WEWORK_INTENT_TIMEOUT_SECONDS"
    )
    wework_intent_min_confidence: float = Field(
        default=0.55, validation_alias="WEWORK_INTENT_MIN_CONFIDENCE"
    )
    # normal=对客 | shadow=审计不发送 AI | disabled=静态话术熔断
    wework_agent_mode: str = Field(default="normal", validation_alias="WEWORK_AGENT_MODE")
    # 上传证件图片时是否多模态识别类型（HKID/PRC_ID/PASSPORT）与号码
    wework_id_vision_enabled: bool = True
    # 视觉进一步判定身份证正反面 + 是否手持拍照（同一调用内，无额外开销）
    # 关闭则 file_field_key 回退「仅按类型」旧行为（HKID/PRC_ID 恒为 id_card_front）
    wework_id_vision_side_classify_enabled: bool = True
    # QA 生成前先发「思考中」提示（占客服额度 1 条；默认关闭）
    wework_thinking_ack_enabled: bool = False
    wework_thinking_ack_text: str = "正在为您查询，请稍候…"
    # 微信客服主动回复额度（48h 窗口；0=不限制）
    wework_kf_send_quota_48h: int = Field(default=5, validation_alias="WEWORK_KF_SEND_QUOTA_48H")
    # 首触达欢迎+清单合并为 1 条（省额度）
    wework_kf_merge_welcome_checklist: bool = True
    # 长答超过该字节数时截断并提示（0=仅按 2048 切分，不摘要）
    wework_kf_long_reply_max_bytes: int = Field(
        default=1800, validation_alias="WEWORK_KF_LONG_REPLY_MAX_BYTES"
    )
    # 切分发送间隔（秒），降低连发触限流
    wework_send_chunk_delay_seconds: float = 0.4
    # 未处理 inbox 超过该秒数则恢复重投
    wework_inbox_stale_seconds: int = 120
    wework_inbox_recover_batch: int = 20
    # QA 防抖（秒）：普通合并等待；明确问句用更快值
    wework_qa_debounce_seconds: float = Field(
        default=1.0, validation_alias="WEWORK_QA_DEBOUNCE_SECONDS"
    )
    wework_qa_debounce_fast_seconds: float = Field(
        default=0.4, validation_alias="WEWORK_QA_DEBOUNCE_FAST_SECONDS"
    )

    # H5 材料收集表单
    collect_form_enabled: bool = False  # True 时才暴露 H5 链接与 /collect/form 路由
    collect_form_base_url: str = ""
    collect_form_jwt_secret: str = ""

    # 材料文件根目录：相对路径相对项目根；绝对路径用于生产服务器
    # 本地默认 data/materials；生产示例 /var/lib/finance-ai/materials
    materials_dir: str = Field(default="data/materials", validation_alias="MATERIALS_DIR")
    # 上传限制（字节）；扩展名逗号分隔，小写
    materials_upload_max_bytes: int = Field(
        default=10 * 1024 * 1024, validation_alias="MATERIALS_UPLOAD_MAX_BYTES"
    )
    materials_upload_allowed_ext: str = Field(
        default=".jpg,.jpeg,.png,.pdf,.webp,.gif",
        validation_alias="MATERIALS_UPLOAD_ALLOWED_EXT",
    )
    materials_dedup_enabled: bool = True
    materials_image_quality_enabled: bool = True
    materials_blur_threshold: float = 100.0  # Laplacian 方差阈值
    # LLM 辅助字段提取兜底（优化 10）
    materials_llm_extraction_enabled: bool = True
    materials_llm_extraction_min_fields: int = 2  # 正则提取 < 此值时触发 LLM
    # 智能缺失材料主动提醒（优化 12）
    materials_proactive_reminder_enabled: bool = True
    materials_proactive_reminder_interval: float = 3600.0  # 最短提醒间隔（秒）
    materials_proactive_reminder_every_n_messages: int = 5  # 每 N 条消息触发检查
    materials_proactive_reminder_max_items: int = 3  # 提醒最多列出几个缺失项
    # 材料收集默认值（客户未提供时回填；秘书不对客收集）
    materials_default_contact_email: str = Field(
        default="13828784214@163.com",
        validation_alias="MATERIALS_DEFAULT_CONTACT_EMAIL",
    )
    materials_default_contact_phone: str = Field(
        default="52667282",
        validation_alias="MATERIALS_DEFAULT_CONTACT_PHONE",
    )
    materials_default_share_capital: int = Field(
        default=10000,
        validation_alias="MATERIALS_DEFAULT_SHARE_CAPITAL",
    )
    materials_default_company_secretary: str = Field(
        default="赢态财务集团安排之持牌秘书",
        validation_alias="MATERIALS_DEFAULT_COMPANY_SECRETARY",
    )
    # 证件识别：OCR 号码兜底 + 姓名/号码与文字材料一致性
    materials_id_ocr_fallback: bool = Field(
        default=True, validation_alias="MATERIALS_ID_OCR_FALLBACK"
    )
    materials_id_name_match: bool = Field(
        default=True, validation_alias="MATERIALS_ID_NAME_MATCH"
    )
    materials_id_min_confidence: float = Field(
        default=0.55, validation_alias="MATERIALS_ID_MIN_CONFIDENCE"
    )
    # 对象存储（可选，未配置则使用 materials_dir 本地存储）
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
    captcha_save_debug: bool = False
    # Ollama 本地视觉模型（免费，推荐 qwen2.5vl:7b）
    ollama_base_url: str = "http://localhost:11434"
    ollama_vision_model: str = ""  # 留空则不使用，如 qwen2.5vl:7b

    # 工作流
    dry_run: bool = True
    # True 且 dry_run=False 时才允许点击 ICRIS 最终提交（生产默认仍关闭）
    icris_allow_submit: bool = False
    notify_colleague_open_id: str = ""
    # L2 注册任务队列 Worker（与 wework-external-bot 同进程）
    icris_worker_enabled: bool = True
    icris_worker_poll_seconds: float = 3.0
    icris_job_max_attempts: int = 3
    # 串行执行，避免多浏览器冲突（预留，当前固定按 1 处理）
    icris_worker_concurrency: int = 1
    # 失败重试退避基数（秒）：delay = base * 2^(attempt-1)
    icris_job_retry_backoff_seconds: float = 30.0

    # ICRIS 账号凭证生成（用户名=Yingtai+月日MMDD+N位随机，密码=用户名+后缀）
    icris_credential_mode: str = "yingtai"  # "yingtai" 新规则 | "legacy" 旧规则
    icris_username_prefix: str = "Yingtai"  # 用户名前缀
    icris_username_random_length: int = 4  # 月日后追加的随机字符数
    icris_username_timestamp_digits: int = 4  # 兼容旧名；未设 random_length 时作回退
    icris_password_suffix: str = "@1"  # 密码后缀（拼在用户名后）
    # ICRIS 注册是否跳过「電子查冊」+「主要账户」选择（默认跳过；False 回退旧行为）
    icris_skip_esearch_principal: bool = True

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
    rag_mmr_enabled: bool = True
    rag_mmr_lambda: float = 0.6  # 0=最大多样性, 1=最大相关性
    rag_exclude_patterns: str = "~$*,*.docx"
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_api_key: str = ""
    qdrant_collection: str = "finance_knowledge"

    # OpenAI 请求超时（秒），避免 Timer 线程挂死
    openai_timeout_seconds: float = Field(
        default=20.0, validation_alias="OPENAI_TIMEOUT_SECONDS"
    )

    # QA Agent Loop（检索/回答打分 + 自我纠错）
    agent_max_retries: int = 2
    agent_retrieval_threshold: float = 0.55
    agent_retrieval_llm_threshold: float = 0.45
    agent_answer_faithfulness_threshold: float = 0.7
    agent_answer_completeness_threshold: float = 0.6
    agent_answer_llm_threshold: float = 0.65
    # Embedding 忠实度融合（优化 3）
    agent_embedding_faithfulness_enabled: bool = True
    agent_embedding_faithfulness_weight: float = 0.4  # 0=仅 bigram, 1=仅 embedding
    # 多策略重试链 rewrite -> relax_scope -> keyword_extract（优化 2）
    agent_multi_strategy_retry_enabled: bool = True
    # 回答一致性检查（优化 4）：与历史相似问题回答矛盾时追加免责声明
    agent_consistency_check_enabled: bool = True
    agent_consistency_similarity_threshold: float = 0.85  # 问题 embedding 相似度阈值
    agent_consistency_history_limit: int = 20  # 取最近 N 条历史回复比对
    agent_consistency_append_disclaimer: bool = True
    # 检索分达到该值：跳过 rewrite；答案启发式过线则跳过 LLM judge
    agent_high_confidence_skip_rewrite: float = 0.70
    agent_high_confidence_skip_judge: float = 0.75
    agent_abstain_on_low_confidence: bool = True
    agent_escalate_to_human: bool = False
    agent_enable_llm_judge: bool = True
    agent_llm_judge_always: bool = False
    agent_log_runs: bool = True
    # 生产建议：域内失败给客户可见兜底，勿裸静默
    agent_silent_on_no_answer: bool = False
    # 弱证据上下文兜底：生产默认关闭，避免幻觉
    agent_contextual_fallback: bool = False
    agent_context_history_limit: int = 10
    agent_abstain_message_to_customer: bool = True
    agent_abstain_message: str = (
        "这个问题我暂时无法准确确认，已记录，专员稍后跟进；您也可回复「转人工」。"
    )
    # 软知识最低检索分，低于则不走弱命中答题
    agent_soft_knowledge_min_score: float = 0.45
    # 高频 FAQ 缓存（命中跳过 RAG/生成）
    agent_faq_enabled: bool = True
    agent_faq_path: str = Field(
        default="docs/knowledge/faq.json", validation_alias="AGENT_FAQ_PATH"
    )
    # 对客回复末尾附短引用
    agent_show_citations: bool = True
    # 自适应回答长度控制（优化 5）
    agent_response_max_bytes: int = 1800  # 0=不限制
    agent_response_summarize_enabled: bool = True

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
        return bool(
            self.wework_corp_id
            and self.wework_external_callback_token_resolved
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

    def _parse_kf_accounts_json(self) -> list[KfAccountConfig]:
        raw = (self.wework_kf_accounts_json or "").strip()
        if not raw:
            legacy = (self.wework_kf_open_kfid or "").strip()
            if legacy:
                return [KfAccountConfig(open_kfid=legacy, name="默认客服")]
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, list):
            return []
        accounts: list[KfAccountConfig] = []
        for item in data:
            if isinstance(item, dict) and (item.get("open_kfid") or "").strip():
                accounts.append(KfAccountConfig.model_validate(item))
        return accounts

    @property
    def wework_kf_accounts(self) -> list[KfAccountConfig]:
        return self._parse_kf_accounts_json()

    @property
    def wework_kf_open_kfid_list(self) -> list[str]:
        return [a.open_kfid for a in self.wework_kf_accounts]

    @property
    def wework_kf_default_open_kfid(self) -> str:
        ids = self.wework_kf_open_kfid_list
        return ids[0] if ids else (self.wework_kf_open_kfid or "").strip()

    def get_kf_account(self, open_kfid: str) -> KfAccountConfig | None:
        for acc in self.wework_kf_accounts:
            if acc.open_kfid == open_kfid:
                return acc
        return None

    @property
    def wework_kf_mode_resolved(self) -> str:
        mode = (self.wework_kf_mode or "both").strip().lower()
        if mode in ("push", "poll", "both"):
            return mode
        return "both"

    @property
    def wework_kf_push_enabled(self) -> bool:
        return self.wework_kf_mode_resolved in ("push", "both")

    @property
    def wework_kf_poll_enabled(self) -> bool:
        return (
            self.wework_kf_sync_enabled
            and self.wework_kf_mode_resolved in ("poll", "both")
        )

    @property
    def wework_kf_configured(self) -> bool:
        return bool(
            self.wework_corp_id
            and (self.wework_kf_secret or "").strip()
            and self.wework_kf_open_kfid_list
        )

    @property
    def wework_channel_resolved(self) -> str:
        ch = (self.wework_channel or "both").strip().lower()
        if ch in ("group", "kf", "both"):
            return ch
        return "both"

    @property
    def wework_channel_group_enabled(self) -> bool:
        return self.wework_channel_resolved in ("group", "both")

    @property
    def wework_channel_kf_enabled(self) -> bool:
        return self.wework_channel_resolved in ("kf", "both")

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
