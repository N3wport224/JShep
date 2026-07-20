"""
IMAP polling listener. Periodically checks the outreach inbox for new
messages, matches them to a known Lead by sender address, and hands each
new reply to the sentiment triage pipeline.

Deliberately polling-based (rather than IDLE) so it works reliably against
any IMAP provider from inside a container on a simple interval, driven by
Celery beat - see app/celery_app.py and app/tasks/celery_tasks.py.
"""
import email
import imaplib
import logging
from email.header import decode_header
from email.utils import parseaddr

from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.resilience import get_circuit_breaker, resilient_retry
from app.models import Lead, Reply

logger = logging.getLogger(__name__)


def _decode(value: str | None) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    decoded = ""
    for text, charset in parts:
        if isinstance(text, bytes):
            decoded += text.decode(charset or "utf-8", errors="replace")
        else:
            decoded += text
    return decoded


def _extract_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition") or "")
            if content_type == "text/plain" and "attachment" not in disposition:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        return ""
    payload = msg.get_payload(decode=True)
    if not payload:
        return ""
    charset = msg.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


class IMAPListenerError(Exception):
    pass


@resilient_retry(retryable_exceptions=(ConnectionError, TimeoutError))
def _fetch_unseen_raw(imap_host: str, imap_port: int, imap_username: str, imap_password: str, mailbox: str) -> list[bytes]:
    """Connect, log in, and return the raw RFC822 bytes of every unseen
    message. Isolated from DB access so it can be retried cleanly without
    replaying any writes."""
    breaker = get_circuit_breaker(f"imap:{imap_host}:{imap_username}")
    breaker.before_call()
    try:
        raw_messages: list[bytes] = []
        with imaplib.IMAP4_SSL(imap_host, imap_port) as imap:
            imap.login(imap_username, imap_password)
            imap.select(mailbox)
            status, data = imap.search(None, "UNSEEN")
            if status != "OK":
                raise IMAPListenerError(f"IMAP search failed: {status}")

            for uid in data[0].split():
                status, msg_data = imap.fetch(uid, "(RFC822)")
                if status != "OK" or not msg_data or msg_data[0] is None:
                    continue
                raw_messages.append((uid, msg_data[0][1]))
    except imaplib.IMAP4.error as exc:
        breaker.on_failure()
        raise IMAPListenerError(f"IMAP connection/auth error: {exc}") from exc
    except (ConnectionError, TimeoutError):
        breaker.on_failure()
        raise
    else:
        breaker.on_success()
        return raw_messages


def fetch_new_replies(db: Session) -> list[Reply]:
    """Pull unseen messages via IMAP, persist any that match a known lead as
    Reply rows (idempotent on imap_uid), and return the newly created Reply
    objects."""
    settings = get_settings()
    if not settings.imap_host or not settings.imap_username:
        logger.debug("IMAP not configured; skipping poll.")
        return []

    raw_messages = _fetch_unseen_raw(
        settings.imap_host, settings.imap_port, settings.imap_username, settings.imap_password, settings.imap_mailbox
    )

    new_replies: list[Reply] = []
    for uid, raw_email in raw_messages:
        uid_str = uid.decode() if isinstance(uid, bytes) else str(uid)
        msg = email.message_from_bytes(raw_email)
        from_name, from_email = parseaddr(msg.get("From", ""))
        from_email = from_email.lower().strip()

        lead = db.query(Lead).filter(Lead.email.ilike(from_email)).first()
        if not lead:
            logger.info("Ignoring inbound message from unknown sender %s", from_email)
            continue

        existing = db.query(Reply).filter(Reply.imap_uid == uid_str).first()
        if existing:
            continue

        body = _extract_body(msg).strip()
        if not body:
            continue

        reply = Reply(
            lead_id=lead.id,
            raw_subject=_decode(msg.get("Subject")),
            raw_body=body,
            imap_uid=uid_str,
        )
        db.add(reply)
        db.flush()
        new_replies.append(reply)

    db.commit()
    return new_replies
