"""
Per-lead conversation memory. Every outbound send and inbound reply is
appended to ThreadMessage in order, so agentic prompts (see
LLMClient.generate_agentic_reply) can be built from the entire email
exchange with a prospect rather than evaluating the latest message in
isolation.
"""
from sqlalchemy.orm import Session

from app.models import Lead, ThreadMessage, ThreadMessageRole

MAX_THREAD_MESSAGES_IN_CONTEXT = 20


def record_outbound(db: Session, lead: Lead, subject: str, body: str, source_email_message_id: str | None = None) -> ThreadMessage:
    entry = ThreadMessage(
        lead_id=lead.id,
        role=ThreadMessageRole.SDR,
        subject=subject,
        body=body,
        source_email_message_id=source_email_message_id,
    )
    db.add(entry)
    db.flush()
    return entry


def record_inbound(db: Session, lead: Lead, subject: str | None, body: str, source_reply_id: str | None = None) -> ThreadMessage:
    entry = ThreadMessage(
        lead_id=lead.id,
        role=ThreadMessageRole.PROSPECT,
        subject=subject,
        body=body,
        source_reply_id=source_reply_id,
    )
    db.add(entry)
    db.flush()
    return entry


def build_thread_context(db: Session, lead: Lead) -> str:
    """Chronological, oldest-first transcript of the lead's thread, capped to
    the most recent MAX_THREAD_MESSAGES_IN_CONTEXT entries to bound prompt size."""
    messages = (
        db.query(ThreadMessage)
        .filter(ThreadMessage.lead_id == lead.id)
        .order_by(ThreadMessage.created_at.asc())
        .all()
    )
    messages = messages[-MAX_THREAD_MESSAGES_IN_CONTEXT:]
    if not messages:
        return "(no prior thread history)"

    lines = []
    for msg in messages:
        speaker = "Prospect" if msg.role == ThreadMessageRole.PROSPECT else "SDR"
        subject_part = f" (Subject: {msg.subject})" if msg.subject else ""
        lines.append(f"[{speaker}]{subject_part}: {msg.body}")
    return "\n\n".join(lines)
