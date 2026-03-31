"""Application configuration loaded from environment variables."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings for bridge services."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str = Field(alias="TELEGRAM_BOT_TOKEN")
    telegram_mode: Literal["polling", "webhook"] = Field(default="polling", alias="TELEGRAM_MODE")
    telegram_webhook_url: str = Field(default="", alias="TELEGRAM_WEBHOOK_URL")

    app_host: str = Field(default="0.0.0.0", alias="APP_HOST")
    app_port: int = Field(default=8000, alias="APP_PORT")

    database_path: Path = Field(default=Path("./data/bridge.db"), alias="DATABASE_PATH")
    log_dir: Path = Field(default=Path("./logs"), alias="LOG_DIR")
    conversation_history_dir: Path = Field(default=Path("./history_conversations"), alias="CONVERSATION_HISTORY_DIR")

    default_codex_mode: Literal["codex_cli_session", "codex_sdk"] = Field(
        default="codex_cli_session", alias="DEFAULT_CODEX_MODE"
    )
    default_workspace: Path = Field(default=Path("/workspace"), alias="DEFAULT_WORKSPACE")
    allowed_workspaces: str = Field(default="", alias="ALLOWED_WORKSPACES")

    codex_bin: str = Field(default="codex", alias="CODEX_BIN")
    codex_cli_args: str = Field(default="", alias="CODEX_CLI_ARGS")
    codex_message_timeout_seconds: int = Field(default=120, alias="CODEX_MESSAGE_TIMEOUT_SECONDS")
    codex_debug_mode: bool = Field(default=False, alias="CODEX_DEBUG_MODE")
    shared_proxy_url: str = Field(default="", alias="SHARED_PROXY_URL")
    shared_proxy_port: int = Field(default=0, alias="SHARED_PROXY_PORT")
    shared_proxy_scheme: str = Field(default="socks5h", alias="SHARED_PROXY_SCHEME")

    telegram_poll_interval_seconds: float = Field(default=1.5, alias="TELEGRAM_POLL_INTERVAL_SECONDS")
    telegram_long_message_chunk: int = Field(default=3500, alias="TELEGRAM_LONG_MESSAGE_CHUNK")
    telegram_debug_mode: bool = Field(default=False, alias="TELEGRAM_DEBUG_MODE")
    telegram_auto_clear_webhook: bool = Field(default=False, alias="TELEGRAM_AUTO_CLEAR_WEBHOOK")

    @field_validator("database_path", "log_dir", "default_workspace", "conversation_history_dir", mode="before")
    @classmethod
    def _expand_path(cls, value: str | Path) -> Path:
        return Path(value).expanduser().resolve()

    @property
    def allowed_workspace_paths(self) -> list[Path]:
        configured = [Path(item.strip()).expanduser().resolve() for item in self.allowed_workspaces.split(",") if item.strip()]
        paths = configured or [self.default_workspace]
        deduped: list[Path] = []
        seen: set[Path] = set()
        for path in paths:
            if path in seen:
                continue
            seen.add(path)
            deduped.append(path)
        return deduped

    @property
    def shared_effective_proxy_url(self) -> str | None:
        if self.shared_proxy_url.strip():
            return self.shared_proxy_url.strip()
        if self.shared_proxy_port > 0:
            return f"{self.shared_proxy_scheme}://127.0.0.1:{self.shared_proxy_port}"
        return None


settings = Settings()
