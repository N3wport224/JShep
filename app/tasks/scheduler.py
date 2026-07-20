"""
Background scheduler: periodically polls IMAP for replies and advances the
multi-step follow-up sequence for leads that haven't replied yet.
"""
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import get_settings
from app.database import SessionLocal
from app.models import EmailMessage, Lead, LeadStatus, MessageDirection, MessageStatus
from app.services.email_sender import EmailSendError, send_email
from app.services.imap_listener import IMAPListenerError, fetch_new_replies
from app.services.llm import LLMJSONError, get_llm_client
from app.services.sentiment import triage_reply

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def _parse_follow_up_delays() -> list[int]:
    settings = get_settings()
    return [int(x.strip()) for x in settings.follow_up_delays_days.split(",") if x.strip()]


def poll_inbox_job() -> None:
    db = SessionLocal()
    try:
        try:
            new_replies = fetch_new_replies(db)
        except IMAPListenerError as exc:
            logger.error("IMAP poll failed: %s", exc)
            return
        for reply in new_replies:
            triage_reply(db, reply)
        if new_replies:
            logger.info("Triaged %d new inbound replies", len(new_replies))
    finally:
        db.close()


def _send_follow_up(db, lead: Lead, step: int, previous_message: EmailMessage) -> None:
    llm = get_llm_client()
    lead_dict = {"company_name": lead.company_name, "contact_name": lead.contact_name}
    try:
        generated = llm.generate_follow_up_email(lead_dict, step, previous_message.body)
    except LLMJSONError as exc:
        logger.error("Follow-up generation failed for lead %s: %s", lead.id, exc)
        return

    message = EmailMessage(
        lead_id=lead.id,
        direction=MessageDirection.OUTBOUND,
        sequence_step=step,
        subject=generated["subject"],
        body=generated["body"],
        status=MessageStatus.DRAFT,
    )
    db.add(message)
    db.flush()

    try:
        message_id_header = send_email(
            to_email=lead.email,
            subject=message.subject,
            body_text=message.body,
            tracking_id=message.tracking_id,
            in_reply_to=previous_message.message_id_header,
        )
    except EmailSendError as exc:
        logger.error("Failed to send follow-up #%d to lead %s: %s", step, lead.id, exc)
        message.status = MessageStatus.FAILED
        db.commit()
        return

    message.status = MessageStatus.SENT
    message.message_id_header = message_id_header
    message.sent_at = datetime.utcnow()
    lead.follow_up_step = step

    delays = _parse_follow_up_delays()
    if step < len(delays):
        lead.next_follow_up_at = datetime.utcnow() + timedelta(days=delays[step])
    else:
        lead.next_follow_up_at = None
    db.commit()


def follow_up_sequence_job() -> None:
    """Advance the follow-up sequence for leads that were sent to / opened
    but never replied and are due for their next step."""
    delays = _parse_follow_up_delays()
    if not delays:
        return

    db = SessionLocal()
    try:
        due_leads = (
            db.query(Lead)
            .filter(
                Lead.status.in_([LeadStatus.SENT, LeadStatus.OPENED]),
                Lead.next_follow_up_at.isnot(None),
                Lead.next_follow_up_at <= datetime.utcnow(),
                Lead.follow_up_step < len(delays),
            )
            .all()
        )
        for lead in due_leads:
            last_message = (
                db.query(EmailMessage)
                .filter(EmailMessage.lead_id == lead.id, EmailMessage.direction == MessageDirection.OUTBOUND)
                .order_by(EmailMessage.created_at.desc())
                .first()
            )
            if not last_message:
                continue
            _send_follow_up(db, lead, lead.follow_up_step + 1, last_message)
    finally:
        db.close()


def schedule_first_follow_up(lead: Lead) -> None:
    """Call after the initial cold email is sent to arm the follow-up sequence."""
    delays = _parse_follow_up_delays()
    if delays:
        lead.next_follow_up_at = datetime.utcnow() + timedelta(days=delays[0])


def start_scheduler() -> BackgroundScheduler | None:
    global _scheduler
    settings = get_settings()
    if not settings.enable_scheduler:
        logger.info("Scheduler disabled via ENABLE_SCHEDULER=false")
        return None
    if _scheduler is not None:
        return _scheduler

    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(
        poll_inbox_job,
        "interval",
        seconds=settings.imap_poll_interval_seconds,
        id="poll_inbox",
        max_instances=1,
        coalesce=True,
    )
    _scheduler.add_job(
        follow_up_sequence_job,
        "interval",
        minutes=30,
        id="follow_up_sequence",
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    logger.info("Background scheduler started.")
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
