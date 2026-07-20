"""
Celery task bodies. All heavy lifting - LLM calls, SMTP/IMAP I/O - happens
here, on the sync engine (app.db_sync), never inline on a FastAPI request.
The web layer only ever enqueues these via app.tasks.dispatch.enqueue.
"""
from datetime import datetime, timedelta

from app.celery_app import celery_app
from app.core.logging import get_logger
from app.core.resilience import CircuitBreakerOpenError
from app.db_sync import SessionLocalSync
from app.models import (
    ApprovalRequest,
    ApprovalStatus,
    EmailMessage,
    Lead,
    LeadStatus,
    MessageDirection,
    MessageStatus,
    Reply,
    SenderAccount,
)
from app.services.campaigns import assign_variant, record_sent
from app.services.email_sender import EmailSendError
from app.services.imap_listener import IMAPListenerError, fetch_new_replies
from app.services.llm import LLMJSONError, get_llm_client
from app.services.memory import record_outbound
from app.services.sender_rotation import NoHealthySenderError, send_via_rotation
from app.services.sentiment import triage_reply
from app.services.suppression import is_suppressed, unsubscribe_footer
from app.tasks.dispatch import with_request_context

logger = get_logger(__name__)


def _parse_follow_up_delays(settings) -> list[int]:
    return [int(x.strip()) for x in settings.follow_up_delays_days.split(",") if x.strip()]


@celery_app.task(name="app.tasks.celery_tasks.enrich_lead_task", bind=True, max_retries=3, default_retry_delay=15)
@with_request_context
def enrich_lead_task(self, lead_id: str) -> dict:
    db = SessionLocalSync()
    try:
        lead = db.get(Lead, lead_id)
        if not lead:
            logger.warning("enrich_lead_task: lead not found", lead_id=lead_id)
            return {"lead_id": lead_id, "status": "not_found"}

        if is_suppressed(db, lead.email):
            logger.info("enrich_lead_task: lead is suppressed, skipping", lead_id=lead_id)
            lead.status = LeadStatus.OPTED_OUT
            db.commit()
            return {"lead_id": lead_id, "status": "suppressed"}

        variant = assign_variant(db, lead)

        llm = get_llm_client()
        try:
            generated = llm.generate_cold_email(
                {
                    "company_name": lead.company_name,
                    "contact_name": lead.contact_name,
                    "website": lead.website,
                    "linkedin_url": lead.linkedin_url,
                },
                variant_hint=variant.prompt_hint if variant else None,
            )
        except LLMJSONError as exc:
            logger.error("enrich_lead_task: LLM failed", lead_id=lead_id, error=str(exc))
            raise self.retry(exc=exc)

        message = EmailMessage(
            lead_id=lead.id,
            direction=MessageDirection.OUTBOUND,
            sequence_step=0,
            subject=generated.subject,
            body=generated.body + unsubscribe_footer(lead),
            status=MessageStatus.DRAFT,
        )
        db.add(message)
        lead.status = LeadStatus.ENRICHED
        db.commit()
        logger.info("lead_enriched", lead_id=lead_id, message_id=message.id, variant=variant.label if variant else None)
        return {"lead_id": lead_id, "message_id": message.id, "status": "enriched"}
    finally:
        db.close()


@celery_app.task(name="app.tasks.celery_tasks.batch_enrich_leads_task")
@with_request_context
def batch_enrich_leads_task(lead_ids: list[str]) -> dict:
    results = [enrich_lead_task.run(lead_id) for lead_id in lead_ids]
    return {"count": len(results), "results": results}


@celery_app.task(name="app.tasks.celery_tasks.send_email_task", bind=True, max_retries=5, default_retry_delay=20)
@with_request_context
def send_email_task(self, message_id: str) -> dict:
    from app.config import get_settings
    from app.tasks.scheduler_logic import arm_first_follow_up

    db = SessionLocalSync()
    try:
        message = db.get(EmailMessage, message_id)
        if not message:
            return {"message_id": message_id, "status": "not_found"}
        if message.status not in (MessageStatus.DRAFT, MessageStatus.QUEUED):
            return {"message_id": message_id, "status": str(message.status)}

        lead: Lead = message.lead

        if is_suppressed(db, lead.email):
            logger.info("send_email_task: lead is suppressed, skipping send", message_id=message_id, lead_id=lead.id)
            message.status = MessageStatus.FAILED
            lead.status = LeadStatus.OPTED_OUT
            db.commit()
            return {"message_id": message_id, "status": "suppressed"}

        message.status = MessageStatus.QUEUED
        db.commit()

        last_outbound = (
            db.query(EmailMessage)
            .filter(
                EmailMessage.lead_id == lead.id,
                EmailMessage.direction == MessageDirection.OUTBOUND,
                EmailMessage.id != message.id,
                EmailMessage.status == MessageStatus.SENT,
            )
            .order_by(EmailMessage.created_at.desc())
            .first()
        )

        try:
            message_id_header, account = send_via_rotation(
                db,
                to_email=lead.email,
                subject=message.subject,
                body_text=message.body,
                tracking_id=message.tracking_id,
                in_reply_to=last_outbound.message_id_header if last_outbound else None,
            )
        except NoHealthySenderError as exc:
            logger.error("send_email_task: no healthy sender", message_id=message_id, error=str(exc))
            message.status = MessageStatus.FAILED
            db.commit()
            return {"message_id": message_id, "status": "failed", "reason": str(exc)}
        except (EmailSendError, CircuitBreakerOpenError) as exc:
            logger.warning("send_email_task: send failed, retrying", message_id=message_id, error=str(exc))
            message.status = MessageStatus.FAILED
            db.commit()
            raise self.retry(exc=exc)

        message.status = MessageStatus.SENT
        message.message_id_header = message_id_header
        message.sender_account_id = account.id
        message.sent_at = datetime.utcnow()
        lead.status = LeadStatus.SENT
        record_outbound(db, lead, message.subject, message.body, source_email_message_id=message.id)
        record_sent(db, lead)

        if message.sequence_step == 0:
            arm_first_follow_up(get_settings(), lead)

        db.commit()
        logger.info("email_sent", message_id=message_id, lead_id=lead.id, sender_account=account.name)
        return {"message_id": message_id, "status": "sent"}
    finally:
        db.close()


@celery_app.task(name="app.tasks.celery_tasks.poll_imap_task")
@with_request_context
def poll_imap_task() -> dict:
    db = SessionLocalSync()
    try:
        try:
            new_replies = fetch_new_replies(db)
        except IMAPListenerError as exc:
            logger.error("poll_imap_task: IMAP poll failed", error=str(exc))
            return {"status": "error", "error": str(exc)}

        reply_ids = [r.id for r in new_replies]
        for reply_id in reply_ids:
            triage_reply_task.delay(reply_id)

        if reply_ids:
            logger.info("imap_poll_found_replies", count=len(reply_ids))
        return {"status": "ok", "new_replies": len(reply_ids)}
    finally:
        db.close()


@celery_app.task(name="app.tasks.celery_tasks.triage_reply_task", bind=True, max_retries=3, default_retry_delay=15)
@with_request_context
def triage_reply_task(self, reply_id: str) -> dict:
    db = SessionLocalSync()
    try:
        reply = db.get(Reply, reply_id)
        if not reply:
            return {"reply_id": reply_id, "status": "not_found"}
        triage_reply(db, reply)
        return {"reply_id": reply_id, "sentiment": reply.sentiment.value if reply.sentiment else None}
    finally:
        db.close()


@celery_app.task(name="app.tasks.celery_tasks.follow_up_sequence_task")
@with_request_context
def follow_up_sequence_task() -> dict:
    from app.config import get_settings

    settings = get_settings()
    delays = _parse_follow_up_delays(settings)
    if not delays:
        return {"status": "no_sequence_configured"}

    db = SessionLocalSync()
    sent = 0
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
        llm = get_llm_client()
        for lead in due_leads:
            if is_suppressed(db, lead.email):
                logger.info("follow_up_sequence_task: lead is suppressed, skipping", lead_id=lead.id)
                lead.status = LeadStatus.OPTED_OUT
                lead.next_follow_up_at = None
                db.commit()
                continue

            last_message = (
                db.query(EmailMessage)
                .filter(
                    EmailMessage.lead_id == lead.id,
                    EmailMessage.direction == MessageDirection.OUTBOUND,
                    EmailMessage.status == MessageStatus.SENT,
                )
                .order_by(EmailMessage.created_at.desc())
                .first()
            )
            if not last_message:
                continue

            step = lead.follow_up_step + 1
            try:
                generated = llm.generate_follow_up_email(
                    {"company_name": lead.company_name, "contact_name": lead.contact_name}, step, last_message.body
                )
            except LLMJSONError as exc:
                logger.error("follow_up_sequence_task: LLM failed", lead_id=lead.id, error=str(exc))
                continue

            message = EmailMessage(
                lead_id=lead.id,
                direction=MessageDirection.OUTBOUND,
                sequence_step=step,
                subject=generated.subject,
                body=generated.body + unsubscribe_footer(lead),
                status=MessageStatus.DRAFT,
            )
            db.add(message)
            db.flush()
            db.commit()
            send_email_task.delay(message.id)
            lead.follow_up_step = step
            lead.next_follow_up_at = (
                datetime.utcnow() + timedelta(days=delays[step]) if step < len(delays) else None
            )
            db.commit()
            sent += 1
        return {"status": "ok", "follow_ups_queued": sent}
    finally:
        db.close()


@celery_app.task(name="app.tasks.celery_tasks.bounce_health_check_task")
@with_request_context
def bounce_health_check_task() -> dict:
    from app.config import get_settings

    settings = get_settings()
    db = SessionLocalSync()
    try:
        accounts = db.query(SenderAccount).all()
        at_risk = []
        for account in accounts:
            if account.is_paused:
                continue
            if account.sent_count >= settings.bounce_rate_min_sample and account.bounce_rate >= (
                settings.bounce_rate_pause_threshold * 0.8
            ):
                at_risk.append({"account": account.name, "bounce_rate": round(account.bounce_rate, 4)})
        if at_risk:
            logger.warning("sender_accounts_at_risk", accounts=at_risk)
        return {"checked": len(accounts), "at_risk": at_risk}
    finally:
        db.close()


@celery_app.task(
    name="app.tasks.celery_tasks.send_approved_reply_task", bind=True, max_retries=5, default_retry_delay=20
)
@with_request_context
def send_approved_reply_task(self, approval_id: str) -> dict:
    """
    Sends the reply for an ApprovalRequest that a human has already marked
    APPROVED (see routers.approvals - the row-locked, race-safe transition
    happens there; this task only ever runs after that transition commits).
    Never triggered any other way - there is no path from a raw LLM output
    to this task without a human's approve action in between.
    """
    db = SessionLocalSync()
    try:
        approval = db.get(ApprovalRequest, approval_id)
        if not approval or approval.status != ApprovalStatus.APPROVED:
            return {"approval_id": approval_id, "status": "skipped"}

        lead: Lead = approval.lead
        reply: Reply = approval.reply

        if is_suppressed(db, lead.email):
            logger.info("send_approved_reply_task: lead is suppressed, skipping send", approval_id=approval_id)
            approval.status = ApprovalStatus.REJECTED
            db.commit()
            return {"approval_id": approval_id, "status": "suppressed"}

        final_body = approval.edited_response or approval.drafted_response
        subject = f"Re: {reply.raw_subject}" if reply.raw_subject else "Re: your reply"

        last_outbound = (
            db.query(EmailMessage)
            .filter(
                EmailMessage.lead_id == lead.id,
                EmailMessage.direction == MessageDirection.OUTBOUND,
                EmailMessage.status == MessageStatus.SENT,
            )
            .order_by(EmailMessage.created_at.desc())
            .first()
        )

        message = EmailMessage(
            lead_id=lead.id,
            direction=MessageDirection.OUTBOUND,
            subject=subject,
            body=final_body,
            status=MessageStatus.DRAFT,
        )
        db.add(message)
        db.flush()

        try:
            message_id_header, account = send_via_rotation(
                db,
                to_email=lead.email,
                subject=subject,
                body_text=final_body,
                tracking_id=message.tracking_id,
                in_reply_to=last_outbound.message_id_header if last_outbound else None,
            )
        except NoHealthySenderError as exc:
            logger.error("send_approved_reply_task: no healthy sender", approval_id=approval_id, error=str(exc))
            message.status = MessageStatus.FAILED
            db.commit()
            return {"approval_id": approval_id, "status": "failed", "reason": str(exc)}
        except (EmailSendError, CircuitBreakerOpenError) as exc:
            logger.warning("send_approved_reply_task: send failed, retrying", approval_id=approval_id, error=str(exc))
            message.status = MessageStatus.FAILED
            db.commit()
            raise self.retry(exc=exc)

        message.status = MessageStatus.SENT
        message.message_id_header = message_id_header
        message.sender_account_id = account.id
        message.sent_at = datetime.utcnow()
        record_outbound(db, lead, subject, final_body, source_email_message_id=message.id)

        approval.status = ApprovalStatus.SENT
        db.commit()
        logger.info("approved_reply_sent", approval_id=approval_id, lead_id=lead.id)
        return {"approval_id": approval_id, "status": "sent"}
    finally:
        db.close()
