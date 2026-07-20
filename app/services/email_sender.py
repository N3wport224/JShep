"""Low-level SMTP dispatch with a tracking pixel for open detection.

This module is intentionally connection-parameter-based (rather than reading
global settings) so app.services.sender_rotation can call it once per
SenderAccount in the rotation pool. Each distinct (host, username) pair gets
its own circuit breaker so one bad account can't fail-fast requests bound for
a healthy one.
"""
import logging
import smtplib
from email.message import EmailMessage as MimeEmailMessage
from email.utils import make_msgid

from app.config import get_settings
from app.core.resilience import RateLimitedError, get_circuit_breaker, resilient_retry

logger = logging.getLogger(__name__)


class EmailSendError(Exception):
    pass


def _tracking_pixel_html(body_text: str, tracking_id: str, api_base_url: str) -> str:
    pixel_url = f"{api_base_url}/tracking/open/{tracking_id}.png"
    html_body = "<br>".join(line for line in body_text.splitlines())
    return f'<div>{html_body}</div><img src="{pixel_url}" width="1" height="1" alt="" style="display:none" />'


@resilient_retry(retryable_exceptions=(RateLimitedError, ConnectionError, TimeoutError, smtplib.SMTPConnectError))
def send_email_smtp(
    *,
    smtp_host: str,
    smtp_port: int,
    smtp_username: str | None,
    smtp_password: str | None,
    smtp_use_tls: bool,
    from_email: str,
    from_name: str,
    to_email: str,
    subject: str,
    body_text: str,
    tracking_id: str,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> str:
    """Send an email via SMTP through the given account's credentials.
    Returns the generated Message-ID header value."""
    breaker = get_circuit_breaker(f"smtp:{smtp_host}:{smtp_username or 'anon'}")
    breaker.before_call()

    msg = MimeEmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_email}>"
    msg["To"] = to_email
    message_id = make_msgid()
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to

    msg.set_content(body_text)
    msg.add_alternative(
        _tracking_pixel_html(body_text, tracking_id, get_settings().api_base_url), subtype="html"
    )

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            if smtp_use_tls:
                server.starttls()
            if smtp_username and smtp_password:
                server.login(smtp_username, smtp_password)
            server.send_message(msg)
    except smtplib.SMTPResponseException as exc:
        breaker.on_failure()
        if exc.smtp_code == 421 or exc.smtp_code == 450 or exc.smtp_code == 452:
            # transient / throttling response codes - treat as rate limited so
            # the retry decorator backs off instead of giving up immediately
            raise RateLimitedError(str(exc)) from exc
        logger.error("SMTP send failed for %s via %s: %s", to_email, smtp_host, exc)
        raise EmailSendError(str(exc)) from exc
    except smtplib.SMTPException as exc:
        breaker.on_failure()
        logger.error("SMTP send failed for %s via %s: %s", to_email, smtp_host, exc)
        raise EmailSendError(str(exc)) from exc
    except (ConnectionError, TimeoutError):
        breaker.on_failure()
        raise

    breaker.on_success()
    return message_id
