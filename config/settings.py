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

    # 飞书
    feishu_app_id: str = ""
    feishu_app_secret: str = ""

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

    @property
    def wework_configured(self) -> bool:
        return bool(self.wework_corp_id and self.wework_corp_secret and self.wework_agent_id)

    @property
    def wework_webhook_configured(self) -> bool:
        return self.wework_configured and bool(self.wework_token and self.wework_encoding_aes_key)

    @property
    def feishu_configured(self) -> bool:
        return bool(self.feishu_app_id and self.feishu_app_secret)

    @property
    def email_configured(self) -> bool:
        return bool(self.email_address and self.email_password)


settings = Settings()
