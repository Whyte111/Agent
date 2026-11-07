"""配置与密钥：全项目唯一读取 os.environ 的地方。

规则:
- 代码里不写死任何密钥 / 收件人;
- 缺少 LLM key 时自动降级到 MOCK 模式(mock 模式不产生任何外部调用);
- 缺少 SMTP 配置时自动降级到 dry-run(邮件落到 data/outbox/*.eml, 不真发)。
这样"最小闭环"在没有 key / 没有网络时也能完整跑通并被验证。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_dotenv(ROOT / ".env")


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    raw = _env(key)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # LLM
    llm_base_url: str
    llm_api_key: str = field(repr=False)
    llm_model: str
    llm_timeout: int
    llm_mock_forced: bool
    # SMTP
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_pass: str = field(repr=False)
    smtp_from: str
    smtp_ssl: bool
    mail_dry_run_forced: bool
    # 服务
    host: str
    port: int
    data_dir: Path
    mock_dir: Path
    max_upload_bytes: int
    allow_remote: bool
    # 搜索（百度千帆 AI 搜索）
    search_api_key: str = field(repr=False)
    search_timeout: int

    # ---- 派生状态(健康检查与页面展示用) ----
    @property
    def llm_live(self) -> bool:
        return bool(self.llm_api_key) and not self.llm_mock_forced

    @property
    def llm_mode(self) -> str:
        return "live" if self.llm_live else "mock"

    @property
    def mail_live(self) -> bool:
        return (
            bool(self.smtp_host and self.smtp_user and self.smtp_pass)
            and not self.mail_dry_run_forced
        )

    @property
    def mail_mode(self) -> str:
        return "smtp" if self.mail_live else "dry-run"

    @property
    def search_live(self) -> bool:
        return bool(self.search_api_key)

    @property
    def search_mode(self) -> str:
        return "live" if self.search_live else "mock"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def outbox_dir(self) -> Path:
        return self.data_dir / "outbox"

    @property
    def generated_dir(self) -> Path:
        return self.data_dir / "generated"


def load_settings() -> Settings:
    data_dir = Path(_env("DATA_DIR", str(ROOT / "data")))
    return Settings(
        llm_base_url=_env("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/"),
        llm_api_key=_env("LLM_API_KEY"),
        llm_model=_env("LLM_MODEL", "deepseek-chat"),
        llm_timeout=_env_int("LLM_TIMEOUT", 60),
        llm_mock_forced=_env_bool("LLM_MOCK", False),
        smtp_host=_env("SMTP_HOST"),
        smtp_port=_env_int("SMTP_PORT", 465),
        smtp_user=_env("SMTP_USER"),
        smtp_pass=_env("SMTP_PASS"),
        smtp_from=_env("SMTP_FROM"),
        smtp_ssl=_env_bool("SMTP_SSL", _env_int("SMTP_PORT", 465) == 465),
        mail_dry_run_forced=_env_bool("MAIL_DRY_RUN", False),
        host=_env("APP_HOST", "127.0.0.1"),
        port=_env_int("APP_PORT", 8000),
        data_dir=data_dir,
        mock_dir=Path(_env("MOCK_DIR", str(ROOT / "data" / "mock"))),
        max_upload_bytes=_env_int("MAX_UPLOAD_MB", 20) * 1024 * 1024,
        allow_remote=_env_bool("APP_ALLOW_REMOTE", False),
        search_api_key=_env("SEARCH_API"),
        search_timeout=_env_int("SEARCH_TIMEOUT", 20),
    )


settings = load_settings()
