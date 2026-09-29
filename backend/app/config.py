"""全局配置:从 backend/.env 读取,缺省值适合本地演示。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BACKEND_DIR / ".env")


def _env(key: str, default: str | None = None) -> str | None:
    value = os.getenv(key)
    if value is None or value.strip() == "":
        return default
    return value.strip()


@dataclass
class Settings:
    llm_provider: str
    anthropic_api_key: str | None
    anthropic_model: str
    anthropic_base_url: str | None
    llm_effort: str
    openai_api_key: str | None
    openai_base_url: str
    openai_model: str
    database_url: str
    embedding_api_key: str | None
    embedding_base_url: str | None
    embedding_model: str | None
    kuaidi100_key: str | None
    kuaidi100_customer: str | None
    knowledge_dir: Path
    data_dir: Path
    smtp_host: str | None
    smtp_port: int
    smtp_user: str | None
    smtp_password: str | None
    smtp_ssl: bool
    smtp_from_name: str
    serverchan_key: str | None
    notify_webhook_url: str | None
    notify_default_contact: str | None
    public_base_url: str | None
    sf_partner_id: str | None
    sf_checkword: str | None
    sf_monthly_card: str | None
    sf_base_url: str
    cors_origins: list[str]
    followup_interval: int
    max_tool_iterations: int
    max_tokens: int

    @property
    def model_name(self) -> str:
        if self.llm_provider == "anthropic":
            return self.anthropic_model
        if self.llm_provider == "openai":
            return self.openai_model
        return "mock-demo"


def _absolutize_sqlite_url(url: str, base: Path) -> str:
    """把 SQLite 的相对路径按 backend/ 解析成绝对路径。

    SQLAlchemy 的 sqlite:///xxx 是相对「进程当前工作目录」的,
    所以在项目根目录启动和在 backend/ 里启动会指向不同的文件,
    前者会直接报 unable to open database file。统一成绝对路径,
    在哪个目录启动都指向同一个库。
    """
    if not url.startswith("sqlite"):
        return url
    prefix, _, path = url.partition(":///")
    # sqlite:///:memory: 和已经是绝对路径的(sqlite:////abs/x.db)不动
    if not path or path.startswith("/") or path.startswith(":memory:"):
        return url
    target = (base / path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    return f"{prefix}:///{target}"


def load_settings() -> Settings:
    anthropic_key = _env("ANTHROPIC_API_KEY")
    openai_key = _env("OPENAI_API_KEY")
    provider = _env("LLM_PROVIDER")
    if not provider:
        provider = "anthropic" if anthropic_key else ("openai" if openai_key else "mock")
    data_dir = BACKEND_DIR / "data"
    data_dir.mkdir(exist_ok=True)
    return Settings(
        llm_provider=provider,
        anthropic_api_key=anthropic_key,
        anthropic_model=_env("ANTHROPIC_MODEL", "claude-opus-5"),
        anthropic_base_url=_env("ANTHROPIC_BASE_URL"),
        llm_effort=_env("LLM_EFFORT", "medium"),
        openai_api_key=openai_key,
        openai_base_url=_env("OPENAI_BASE_URL", "https://api.deepseek.com"),
        openai_model=_env("OPENAI_MODEL", "deepseek-chat"),
        database_url=_absolutize_sqlite_url(_env("DATABASE_URL", "sqlite:///./data/app.db"), BACKEND_DIR),
        embedding_api_key=_env("EMBEDDING_API_KEY"),
        embedding_base_url=_env("EMBEDDING_BASE_URL"),
        embedding_model=_env("EMBEDDING_MODEL"),
        kuaidi100_key=_env("KUAIDI100_KEY"),
        kuaidi100_customer=_env("KUAIDI100_CUSTOMER"),
        knowledge_dir=BACKEND_DIR / "knowledge",
        data_dir=data_dir,
        smtp_host=_env("SMTP_HOST"),
        smtp_port=int(_env("SMTP_PORT", "465")),
        smtp_user=_env("SMTP_USER"),
        smtp_password=_env("SMTP_PASSWORD"),
        smtp_ssl=(_env("SMTP_SSL", "true") or "true").lower() not in ("0", "false", "no"),
        smtp_from_name=_env("SMTP_FROM_NAME", "快递管家"),
        serverchan_key=_env("SERVERCHAN_KEY"),
        notify_webhook_url=_env("NOTIFY_WEBHOOK_URL"),
        notify_default_contact=_env("NOTIFY_DEFAULT_CONTACT"),
        public_base_url=_env("PUBLIC_BASE_URL"),
        sf_partner_id=_env("SF_PARTNER_ID"),
        sf_checkword=_env("SF_CHECKWORD"),
        sf_monthly_card=_env("SF_MONTHLY_CARD"),
        sf_base_url=_env("SF_BASE_URL", "https://bspgw.sf-express.com"),
        cors_origins=[o.strip() for o in (_env("CORS_ORIGINS") or "").split(",") if o.strip()],
        followup_interval=int(_env("FOLLOWUP_CHECK_INTERVAL", "10")),
        max_tool_iterations=int(_env("MAX_TOOL_ITERATIONS", "12")),
        max_tokens=int(_env("LLM_MAX_TOKENS", "8000")),
    )


settings = load_settings()
