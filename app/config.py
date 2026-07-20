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
    log_level: str = "INFO"
    log_json: bool = True

    # --- Database (Postgres). Async driver for the FastAPI app, sync driver
    # for Celery workers / Alembic which don't run inside an asyncio loop. ---
    database_url: str = "postgresql+asyncpg://sdr:sdr@postgres:5432/sdr"

    @property
    def sync_database_url(self) -> str:
        """Same database, sync driver, for Celery workers and Alembic. Maps
        the async driver prefix to its sync counterpart; also handles
        sqlite+aiosqlite (used by the test suite) -> sqlite."""
        if self.database_url.startswith("postgresql+asyncpg://"):
            return self.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
        if self.database_url.startswith("sqlite+aiosqlite://"):
            return self.database_url.replace("sqlite+aiosqlite://", "sqlite://")
        return self.database_url

    # --- Redis / Celery ---
    redis_url: str = "redis://redis:6379/0"
    celery_broker_url: Optional[str] = None
    celery_result_backend: Optional[str] = None

    @property
    def resolved_celery_broker_url(self) -> str:
        return self.celery_broker_url or self.redis_url

    @property
    def resolved_celery_result_backend(self) -> str:
        return self.celery_result_backend or self.redis_url

    # --- LLM provider ---
    llm_provider: Literal["anthropic", "openai"] = "anthropic"
    anthropic_api_key: Optional[str] = None
    anthropic_model: str = "claude-sonnet-5"
    openai_api_key: Optional[str] = None
    openai_model: str = "gpt-4o-mini"
    llm_agentic_max_steps: int = 3

    # --- Outbound email (SMTP) - default/fallback sender account. Additional
    # rotation-pool accounts can be configured via SENDER_ACCOUNTS_JSON. ---
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    smtp_username: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_use_tls: bool = True
    from_email: Optional[str] = None
    from_name: str = "Sales Team"
    # JSON array of extra sender accounts, e.g.
    # [{"name":"acct2","smtp_host":"...","smtp_port":587,"smtp_username":"...",
    #   "smtp_password":"...","from_email":"...","from_name":"...","daily_limit":100}]
    sender_accounts_json: Optional[str] = None
    # Pause a sending account automatically once its bounce rate exceeds this.
    bounce_rate_pause_threshold: float = 0.02
    bounce_rate_min_sample: int = 20
    # Pause a sending account automatically once its spam-complaint rate
    # exceeds this (same min-sample guard as bounce rate).
    spam_complaint_rate_pause_threshold: float = 0.001

    # --- Inbox warmup: daily_limit ramps through these stages instead of a
    # new sender account blasting at full volume from day one. Advanced by
    # the daily warmup_rotation_task; also resets sent/bounce/open/spam
    # counters for the new day so rates reflect a rolling daily window
    # rather than all-time totals. ---
    warmup_enabled: bool = True
    warmup_daily_targets: str = "10,20,40,80,150,200"

    # --- Inbound email (IMAP) - default account used by the poller ---
    imap_host: Optional[str] = None
    imap_port: int = 993
    imap_username: Optional[str] = None
    imap_password: Optional[str] = None
    imap_poll_interval_seconds: int = 60
    imap_mailbox: str = "INBOX"

    # --- Outreach platform integrations (alternative to raw SMTP/IMAP) ---
    instantly_api_key: Optional[str] = None
    smartlead_api_key: Optional[str] = None

    # --- Follow-up sequence ---
    follow_up_delays_days: str = "3,7"  # comma separated day offsets for follow-up steps

    # --- Human-in-the-loop notification channels ---
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    discord_webhook_url: Optional[str] = None
    generic_notify_webhook_url: Optional[str] = None

    # --- Security ---
    admin_api_key: Optional[str] = None
    jwt_secret: str = "change-me-please-use-a-long-random-string"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60
    admin_username: Optional[str] = None
    admin_password: Optional[str] = None
    rate_limit_default: str = "60/minute"
    rate_limit_webhook: str = "120/minute"

    # --- CRM integration: push Positive/Interested leads automatically ---
    crm_provider: Literal["none", "hubspot"] = "none"
    hubspot_access_token: Optional[str] = None

    # --- Calendar / meeting scheduling ---
    # A static booking link (Cal.com or a Google Calendar appointment
    # schedule link both work) offered to prospects who ask to book a call.
    calendar_booking_url: Optional[str] = None

    # --- LinkedIn touchpoint automation (see app.services.linkedin_automation) ---
    # Endpoint for a headless-browser automation layer (PhantomBuster, a
    # local Playwright worker) that actually performs profile views/connection
    # requests. Left unset, execution is safely simulated (logged only) so
    # multi-channel sequences work end to end in dev/test without a real
    # browser automation backend.
    linkedin_automation_webhook_url: Optional[str] = None
    linkedin_automation_api_key: Optional[str] = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
