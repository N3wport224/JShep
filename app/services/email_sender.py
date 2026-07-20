"""Outbound email dispatch over SMTP with a tracking pixel for open detection."""
import logging
import smtplib
from email.message import EmailMessage as MimeEmailMessage
from email.utils import make_msgid

from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import get_settings

logger = logging.getLogger(__name__)


class EmailSendError(Exception):
    pass


def _tracking_pixel_html(body_text: str, tracking_id: str, api_base_url: str) -> str:
    pixel_url = f"{api_base_url}/tracking/open/{tracking_id}.png"
    html_body = "<br>".join(line for line in body_text.splitlines())
    return f'<div>{html_body}</div><img src="{pixel_url}" width="1" height="1" alt="" style="display:none" />'


@retry(reraise=True, stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=20))
def send_email(
    to_email: str,
    subject: str,
    body_text: str,
    tracking_id: str,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> str:
    """Send an email via SMTP. Returns the generated Message-ID header value."""
    settings = get_settings()
    if not settings.smtp_host or not settings.from_email:
        raise EmailSendError("SMTP is not configured (SMTP_HOST / FROM_EMAIL missing)")

    msg = MimeEmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{settings.from_name} <{settings.from_email}>"
    msg["To"] = to_email
    message_id = make_msgid()
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to

    msg.set_content(body_text)
    msg.add_alternative(_tracking_pixel_html(body_text, tracking_id, settings.api_base_url), subtype="html")

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
            if settings.smtp_use_tls:
                server.starttls()
            if settings.smtp_username and settings.smtp_password:
                server.login(settings.smtp_username, settings.smtp_password)
            server.send_message(msg)
    except smtplib.SMTPException as exc:
        logger.error("SMTP send failed for %s: %s", to_email, exc)
        raise EmailSendError(str(exc)) from exc

    return message_id
