"""Outbound sending: queue drafted emails for dispatch and view send/open/reply status.
Sending always goes through Celery + the sender rotation manager, never inline.

Every cold email/follow-up passes through the pre-send spam guardian
(app.services.spam_guardian) before it's ever eligible to queue here - a
message still scored HIGH risk after LLM self-correction rewrite attempts
is created as NEEDS_REVIEW instead of DRAFT, and /send/{id} only accepts
DRAFT messages, so a flagged draft can never reach the sending queue without
a human clearing it via POST /outbound/messages/{id}/approve-review first."""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.security import limiter, require_admin
from app.database import get_db
from app.models import EmailMessage, MessageStatus
from app.schemas import EmailMessageOut, ReviewApprovalIn
from app.tasks.celery_tasks import send_email_task
from app.tasks.dispatch import enqueue

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/outbound", tags=["outbound"])


@router.post("/send/{message_id}", status_code=202)
@limiter.limit(get_settings().rate_limit_default)
async def send_message(request: Request, message_id: str, db: AsyncSession = Depends(get_db)):
    """Queue a drafted (enriched) email message for dispatch to its lead."""
    message = await db.get(EmailMessage, message_id)
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")
    if message.status != MessageStatus.DRAFT:
        raise HTTPException(status_code=409, detail=f"Message already in status {message.status}")

    task_id = enqueue(send_email_task, message.id)
    return {"message_id": message.id, "task_id": task_id, "status": "queued"}


@router.get("/messages", response_model=list[EmailMessageOut])
async def list_messages(lead_id: str | None = None, status: MessageStatus | None = None, db: AsyncSession = Depends(get_db)):
    query = select(EmailMessage)
    if lead_id:
        query = query.where(EmailMessage.lead_id == lead_id)
    if status:
        query = query.where(EmailMessage.status == status)
    result = await db.execute(query.order_by(EmailMessage.created_at.desc()))
    return result.scalars().all()


@router.post("/messages/{message_id}/approve-review", response_model=EmailMessageOut, dependencies=[Depends(require_admin)])
async def approve_review(message_id: str, payload: ReviewApprovalIn, db: AsyncSession = Depends(get_db)):
    """Clear a NEEDS_REVIEW message (flagged by the pre-send spam guardian)
    back to DRAFT so it becomes eligible for POST /outbound/send/{id}.
    Optionally pass edited subject/body to fix the flagged copy instead of
    sending it as originally drafted - the message is NOT re-scored, since
    a human has now explicitly reviewed and approved it."""
    message = await db.get(EmailMessage, message_id)
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")
    if message.status != MessageStatus.NEEDS_REVIEW:
        raise HTTPException(status_code=409, detail=f"Message is not pending review (status={message.status})")

    if payload.subject is not None:
        message.subject = payload.subject
    if payload.body is not None:
        message.body = payload.body
    message.status = MessageStatus.DRAFT

    await db.commit()
    await db.refresh(message)
    logger.info("Message %s cleared from spam review by admin", message_id)
    return message
