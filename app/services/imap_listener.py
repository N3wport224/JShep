"""
IMAP polling listener. Periodically checks the outreach inbox for new
messages, matches them to a known Lead by sender address, and hands each
new reply to the sentiment triage pipeline.

Deliberately polling-based (rather than IDLE) so it works reliably against
any IMAP provider from inside a container on a simple interval via
APScheduler - see app/tasks/scheduler.py.
"""
import email
import imaplib
import logging
from email.header import decode_header
from email.utils import parseaddr

from sqlalchemy.orm import Session

from app.config import get_settings
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


def fetch_new_replies(db: Session) -> list[Reply]:
    """Connect to IMAP, pull unseen messages, persist any that match a
    known lead as Reply rows (idempotent on imap_uid), and return the
    newly created Reply objects."""
    settings = get_settings()
    if not settings.imap_host or not settings.imap_username:
        logger.debug("IMAP not configured; skipping poll.")
        return []

    new_replies: list[Reply] = []
    try:
        with imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port) as imap:
            imap.login(settings.imap_username, settings.imap_password)
            imap.select(settings.imap_mailbox)
            status, data = imap.search(None, "UNSEEN")
            if status != "OK":
                raise IMAPListenerError(f"IMAP search failed: {status}")

            uids = data[0].split()
            for uid in uids:
                uid_str = uid.decode()
                status, msg_data = imap.fetch(uid, "(RFC822)")
                if status != "OK" or not msg_data or msg_data[0] is None:
                    continue

                raw_email = msg_data[0][1]
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
    except imaplib.IMAP4.error as exc:
        raise IMAPListenerError(f"IMAP connection/auth error: {exc}") from exc

    return new_replies
