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

    # 钉钉
    dingtalk_app_key: str = ""
    dingtalk_app_secret: str = ""

    # 邮箱
    email_imap_host: str = "imap.example.com"
    email_imap_port: int = 993
    email_address: str = ""
    email_password: str = ""

    # 浏览器
    browser_headless: bool = False
    browser_slow_mo: int = 100
    browser_channel: str = ""  # chrome / msedge / chromium，留空则自动检测
    browser_no_proxy: bool = True  # 绕过系统代理，避免 ERR_PROXY_CONNECTION_FAILED
    browser_keep_open_seconds: int = 60  # 结束或出错后保持浏览器打开的秒数

    # 验证码识别
    # auto=优先2Captcha→OCR回退 | 2captcha=仅打码平台 | audio | ocr | manual
    captcha_mode: str = "auto"
    captcha_manual_timeout: int = 180
    twocaptcha_api_key: str = ""
    twocaptcha_max_variants: int = 5  # 并行提交的 GIF 帧变体数
    twocaptcha_timeout: int = 120  # 单次 2Captcha 任务超时（秒）
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
    def dingtalk_configured(self) -> bool:
        return bool(self.dingtalk_app_key and self.dingtalk_app_secret)

    @property
    def email_configured(self) -> bool:
        return bool(self.email_address and self.email_password)


settings = Settings()
