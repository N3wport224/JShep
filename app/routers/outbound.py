"""Outbound sending: queue drafted emails for dispatch and view send/open/reply status.
Sending always goes through Celery + the sender rotation manager, never inline."""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.security import limiter
from app.database import get_db
from app.models import EmailMessage, MessageStatus
from app.schemas import EmailMessageOut
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
async def list_messages(lead_id: str | None = None, db: AsyncSession = Depends(get_db)):
    query = select(EmailMessage)
    if lead_id:
        query = query.where(EmailMessage.lead_id == lead_id)
    result = await db.execute(query.order_by(EmailMessage.created_at.desc()))
    return result.scalars().all()
