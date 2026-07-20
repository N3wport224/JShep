"""Centralized application settings loaded from environment variables."""
from functools import lru_cache
from typing import Literal, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- App ---
    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    api_base_url: str = "http://localhost:8000"
    database_url: str = "sqlite:////data/sdr.db"

    # --- LLM provider ---
    llm_provider: Literal["anthropic", "openai"] = "anthropic"
    anthropic_api_key: Optional[str] = None
    anthropic_model: str = "claude-sonnet-5"
    openai_api_key: Optional[str] = None
    openai_model: str = "gpt-4o-mini"

    # --- Outbound email (SMTP) ---
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    smtp_username: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_use_tls: bool = True
    from_email: Optional[str] = None
    from_name: str = "Sales Team"

    # --- Inbound email (IMAP) ---
    imap_host: Optional[str] = None
    imap_port: int = 993
    imap_username: Optional[str] = None
    imap_password: Optional[str] = None
    imap_poll_interval_seconds: int = 60
    imap_mailbox: str = "INBOX"

    # --- Follow-up sequence ---
    follow_up_delays_days: str = "3,7"  # comma separated day offsets for follow-up steps

    # --- Human-in-the-loop notification channels ---
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    discord_webhook_url: Optional[str] = None
    generic_notify_webhook_url: Optional[str] = None

    # --- Scheduler ---
    enable_scheduler: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
