"""Outbound sending: dispatch drafted emails and view send/open/reply status."""
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import EmailMessage, Lead, LeadStatus, MessageStatus
from app.schemas import EmailMessageOut
from app.services.email_sender import EmailSendError, send_email
from app.tasks.scheduler import schedule_first_follow_up

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/outbound", tags=["outbound"])


@router.post("/send/{message_id}", response_model=EmailMessageOut)
def send_message(message_id: str, db: Session = Depends(get_db)):
    """Send a drafted (enriched) email message to its lead."""
    message = db.get(EmailMessage, message_id)
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")
    if message.status != MessageStatus.DRAFT:
        raise HTTPException(status_code=409, detail=f"Message already in status {message.status}")

    lead: Lead = message.lead
    try:
        message_id_header = send_email(
            to_email=lead.email,
            subject=message.subject,
            body_text=message.body,
            tracking_id=message.tracking_id,
        )
    except EmailSendError as exc:
        message.status = MessageStatus.FAILED
        db.commit()
        raise HTTPException(status_code=502, detail=f"Failed to send email: {exc}") from exc

    message.status = MessageStatus.SENT
    message.message_id_header = message_id_header
    message.sent_at = datetime.utcnow()
    lead.status = LeadStatus.SENT
    if message.sequence_step == 0:
        schedule_first_follow_up(lead)
    db.commit()
    db.refresh(message)
    return message


@router.get("/messages", response_model=list[EmailMessageOut])
def list_messages(lead_id: str | None = None, db: Session = Depends(get_db)):
    query = db.query(EmailMessage)
    if lead_id:
        query = query.filter(EmailMessage.lead_id == lead_id)
    return query.order_by(EmailMessage.created_at.desc()).all()
