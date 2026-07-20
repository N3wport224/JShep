"""
Incoming reply handling. Two paths:
  1. POST /inbound/poll - manually trigger an IMAP poll (also run on a
     schedule, see app/tasks/scheduler.py).
  2. POST /inbound/webhook - generic webhook for outreach platforms
     (e.g. Instantly/Smartlead) that push replies instead of using IMAP.
Both paths converge on the same sentiment triage pipeline.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Lead, Reply
from app.schemas import ReplyOut
from app.services.imap_listener import IMAPListenerError, fetch_new_replies
from app.services.sentiment import triage_reply

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/inbound", tags=["inbound"])


@router.post("/poll", response_model=list[ReplyOut])
def poll_imap(db: Session = Depends(get_db)):
    try:
        new_replies = fetch_new_replies(db)
    except IMAPListenerError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    for reply in new_replies:
        triage_reply(db, reply)
    return new_replies


class InboundWebhookPayload(BaseModel):
    lead_email: EmailStr
    subject: str | None = None
    body: str


@router.post("/webhook", response_model=ReplyOut)
def inbound_webhook(payload: InboundWebhookPayload, db: Session = Depends(get_db)):
    """Receive a reply pushed from an outreach platform webhook (instead of IMAP)."""
    lead = db.query(Lead).filter(Lead.email.ilike(payload.lead_email)).first()
    if not lead:
        raise HTTPException(status_code=404, detail=f"No lead found for {payload.lead_email}")

    reply = Reply(lead_id=lead.id, raw_subject=payload.subject, raw_body=payload.body)
    db.add(reply)
    db.flush()
    reply = triage_reply(db, reply)
    return reply
