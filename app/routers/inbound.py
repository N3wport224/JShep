"""
Incoming reply handling. Two paths:
  1. POST /inbound/poll - manually trigger an IMAP poll (admin-only; also
     runs on Celery beat's schedule automatically).
  2. POST /inbound/webhook - generic webhook for outreach platforms
     (e.g. Instantly/Smartlead) that push replies instead of using IMAP.
Both paths converge on the same sentiment triage pipeline, run as a Celery
task so an LLM call never blocks the request/webhook response.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.security import limiter, require_admin
from app.database import get_db
from app.models import Lead, Reply
from app.schemas import ReplyOut
from app.tasks.celery_tasks import poll_imap_task, triage_reply_task
from app.tasks.dispatch import enqueue

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/inbound", tags=["inbound"])


@router.post("/poll", status_code=202, dependencies=[Depends(require_admin)])
@limiter.limit("10/minute")
async def poll_imap(request: Request):
    task_id = enqueue(poll_imap_task)
    return {"task_id": task_id, "status": "queued"}


class InboundWebhookPayload(BaseModel):
    lead_email: EmailStr
    subject: str | None = None
    body: str


@router.post("/webhook", response_model=ReplyOut, status_code=202)
@limiter.limit(get_settings().rate_limit_webhook)
async def inbound_webhook(request: Request, payload: InboundWebhookPayload, db: AsyncSession = Depends(get_db)):
    """Receive a reply pushed from an outreach platform webhook (instead of IMAP)."""
    result = await db.execute(select(Lead).where(Lead.email.ilike(payload.lead_email)))
    lead = result.scalar_one_or_none()
    if not lead:
        raise HTTPException(status_code=404, detail=f"No lead found for {payload.lead_email}")

    reply = Reply(lead_id=lead.id, raw_subject=payload.subject, raw_body=payload.body)
    db.add(reply)
    await db.commit()
    await db.refresh(reply)

    enqueue(triage_reply_task, reply.id)
    return reply
