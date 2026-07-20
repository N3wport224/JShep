"""
Multi-provider outbound sender pool. Abstracts over raw SMTP accounts and
API-based outreach platforms (Instantly, Smartlead) behind one interface, and
picks a healthy, non-paused account for each send using least-recently-used
rotation. Bounce tracking here auto-pauses an account once its bounce rate
crosses BOUNCE_RATE_PAUSE_THRESHOLD, protecting domain reputation instead of
letting a failing account keep sending.
"""
import abc
import logging
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.metrics import email_dispatch_total, sender_bounce_rate
from app.core.resilience import RateLimitedError, get_circuit_breaker, resilient_retry
from app.models import MessageStatus, SenderAccount, SenderProvider
from app.services.email_sender import EmailSendError, send_email_smtp

logger = logging.getLogger(__name__)


class NoHealthySenderError(Exception):
    """Raised when every configured sender account is paused or over its
    daily limit - nothing safe to send from."""


class ProviderSender(abc.ABC):
    """Common interface every outbound provider implements, so the rotation
    manager doesn't need to know whether it's talking to raw SMTP or a
    platform API."""

    @abc.abstractmethod
    def send(self, account: SenderAccount, *, to_email: str, subject: str, body_text: str, tracking_id: str,
              in_reply_to: str | None = None) -> str:
        """Send the email and return a provider message ID / Message-ID header."""


class SMTPProviderSender(ProviderSender):
    def send(self, account: SenderAccount, *, to_email: str, subject: str, body_text: str, tracking_id: str,
              in_reply_to: str | None = None) -> str:
        if not account.smtp_host:
            raise EmailSendError(f"Sender account {account.name!r} has no SMTP host configured")
        return send_email_smtp(
            smtp_host=account.smtp_host,
            smtp_port=account.smtp_port or 587,
            smtp_username=account.smtp_username,
            smtp_password=account.smtp_password,
            smtp_use_tls=account.smtp_use_tls,
            from_email=account.from_email,
            from_name=account.from_name,
            to_email=to_email,
            subject=subject,
            body_text=body_text,
            tracking_id=tracking_id,
            in_reply_to=in_reply_to,
        )


class _OutreachPlatformSender(ProviderSender):
    """Shared HTTP-API sending logic for Instantly/Smartlead-style
    platforms. Both expose a simple "send this email now" REST endpoint
    guarded by an API key; subclasses just fill in the endpoint/payload
    shape. Real integration details vary by account plan, so this is
    intentionally a thin, swappable abstraction rather than a full client."""

    api_base_url: str

    @resilient_retry(retryable_exceptions=(RateLimitedError, ConnectionError, TimeoutError))
    def _post(self, path: str, account: SenderAccount, payload: dict) -> dict:
        breaker = get_circuit_breaker(f"{self.__class__.__name__}:{account.name}")
        breaker.before_call()
        try:
            resp = httpx.post(
                f"{self.api_base_url}{path}",
                json=payload,
                headers={"Authorization": f"Bearer {account.api_key}"},
                timeout=20,
            )
            if resp.status_code == 429:
                raise RateLimitedError(f"{self.__class__.__name__} rate limited: {resp.text}")
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            breaker.on_failure()
            raise EmailSendError(str(exc)) from exc
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            breaker.on_failure()
            raise ConnectionError(str(exc)) from exc
        else:
            breaker.on_success()
            return resp.json()


class InstantlySender(_OutreachPlatformSender):
    api_base_url = "https://api.instantly.ai/api/v2"

    def send(self, account: SenderAccount, *, to_email: str, subject: str, body_text: str, tracking_id: str,
              in_reply_to: str | None = None) -> str:
        data = self._post(
            "/emails/send",
            account,
            {
                "to": to_email,
                "from": account.from_email,
                "subject": subject,
                "body": {"text": body_text},
                "custom_tracking_id": tracking_id,
            },
        )
        return data.get("id", tracking_id)


class SmartleadSender(_OutreachPlatformSender):
    api_base_url = "https://server.smartlead.ai/api/v1"

    def send(self, account: SenderAccount, *, to_email: str, subject: str, body_text: str, tracking_id: str,
              in_reply_to: str | None = None) -> str:
        data = self._post(
            "/campaigns/send-email",
            account,
            {
                "to_email": to_email,
                "from_email": account.from_email,
                "subject": subject,
                "body": body_text,
                "tracking_id": tracking_id,
            },
        )
        return data.get("id", tracking_id)


_PROVIDER_SENDERS: dict[SenderProvider, ProviderSender] = {
    SenderProvider.SMTP: SMTPProviderSender(),
    SenderProvider.INSTANTLY: InstantlySender(),
    SenderProvider.SMARTLEAD: SmartleadSender(),
}


def _pick_account(db: Session) -> SenderAccount:
    settings = get_settings()
    candidates = (
        db.query(SenderAccount)
        .filter(SenderAccount.is_paused.is_(False), SenderAccount.sent_count < SenderAccount.daily_limit)
        .order_by(SenderAccount.last_used_at.asc().nullsfirst())
        .all()
    )
    open_breaker_names = set()
    for account in candidates:
        breaker_name = f"smtp:{account.smtp_host}:{account.smtp_username or 'anon'}" if account.provider == SenderProvider.SMTP else f"{account.provider.value}:{account.name}"
        breaker = get_circuit_breaker(breaker_name)
        if breaker.state.value == "open":
            open_breaker_names.add(account.id)
            continue
        return account

    if not candidates:
        raise NoHealthySenderError(
            "No sender accounts are configured/healthy - all are paused or over their daily limit. "
            f"(bounce pause threshold={settings.bounce_rate_pause_threshold})"
        )
    raise NoHealthySenderError("Every sender account's circuit breaker is currently open (recent failures)")


def send_via_rotation(
    db: Session,
    *,
    to_email: str,
    subject: str,
    body_text: str,
    tracking_id: str,
    in_reply_to: str | None = None,
) -> tuple[str, SenderAccount]:
    """Pick a healthy sender account, send through it, and update its usage
    counters. Returns (message_id_header, account_used). Raises
    NoHealthySenderError if nothing is available, or EmailSendError if the
    send itself fails after retries - callers should mark the message
    FAILED and let a human/retry policy handle it, never fall back to
    sending from an unhealthy/paused account."""
    account = _pick_account(db)
    sender = _PROVIDER_SENDERS[account.provider]

    try:
        message_id = sender.send(
            account, to_email=to_email, subject=subject, body_text=body_text, tracking_id=tracking_id,
            in_reply_to=in_reply_to,
        )
    except Exception:
        account.sent_count += 1  # attempted send counts toward daily volume even on failure
        account.last_used_at = datetime.utcnow()
        email_dispatch_total.labels(status="failed", sender_account=account.name).inc()
        db.commit()
        raise

    account.sent_count += 1
    account.last_used_at = datetime.utcnow()
    email_dispatch_total.labels(status="sent", sender_account=account.name).inc()
    sender_bounce_rate.labels(sender_account=account.name).observe(account.bounce_rate)
    db.commit()
    return message_id, account


def record_bounce(db: Session, account: SenderAccount) -> None:
    """Called when a send/DSN indicates a bounce for messages routed through
    this account. Auto-pauses the account once its bounce rate crosses the
    configured threshold (with a minimum sample size to avoid pausing on a
    single unlucky early bounce)."""
    settings = get_settings()
    account.bounce_count += 1
    sender_bounce_rate.labels(sender_account=account.name).observe(account.bounce_rate)

    if (
        not account.is_paused
        and account.sent_count >= settings.bounce_rate_min_sample
        and account.bounce_rate >= settings.bounce_rate_pause_threshold
    ):
        account.is_paused = True
        account.pause_reason = (
            f"Auto-paused: bounce rate {account.bounce_rate:.1%} exceeded "
            f"threshold {settings.bounce_rate_pause_threshold:.1%} "
            f"({account.bounce_count}/{account.sent_count} sent)"
        )
        logger.warning("Sender account %s auto-paused: %s", account.name, account.pause_reason)

    db.commit()


def seed_sender_accounts_from_env(db: Session) -> None:
    """Populate the sender_accounts table from env config on startup, if
    empty. SMTP_* becomes the "default" account; SENDER_ACCOUNTS_JSON adds
    any extra accounts to the rotation pool."""
    import json

    settings = get_settings()
    if db.query(SenderAccount).count() > 0:
        return

    accounts = []
    if settings.smtp_host and settings.from_email:
        accounts.append(
            SenderAccount(
                name="default",
                provider=SenderProvider.SMTP,
                from_email=settings.from_email,
                from_name=settings.from_name,
                smtp_host=settings.smtp_host,
                smtp_port=settings.smtp_port,
                smtp_username=settings.smtp_username,
                smtp_password=settings.smtp_password,
                smtp_use_tls=settings.smtp_use_tls,
            )
        )

    if settings.sender_accounts_json:
        try:
            extra = json.loads(settings.sender_accounts_json)
        except json.JSONDecodeError:
            logger.error("SENDER_ACCOUNTS_JSON is not valid JSON; skipping extra sender accounts")
            extra = []
        for entry in extra:
            accounts.append(
                SenderAccount(
                    name=entry["name"],
                    provider=SenderProvider(entry.get("provider", "smtp")),
                    from_email=entry.get("from_email", settings.from_email),
                    from_name=entry.get("from_name", settings.from_name),
                    smtp_host=entry.get("smtp_host"),
                    smtp_port=entry.get("smtp_port"),
                    smtp_username=entry.get("smtp_username"),
                    smtp_password=entry.get("smtp_password"),
                    smtp_use_tls=entry.get("smtp_use_tls", True),
                    api_key=entry.get("api_key"),
                    daily_limit=entry.get("daily_limit", 200),
                )
            )

    if accounts:
        db.add_all(accounts)
        db.commit()
        logger.info("Seeded %d sender account(s) from environment config", len(accounts))
