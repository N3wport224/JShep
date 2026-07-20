"""Open tracking via a 1x1 pixel embedded in outbound HTML emails."""
from datetime import datetime

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import EmailMessage, Lead, LeadStatus, MessageStatus

router = APIRouter(prefix="/tracking", tags=["tracking"])

# 1x1 transparent PNG
_PIXEL_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6360000002000155e21a7e0000000049454e44ae426082"
)


@router.get("/open/{tracking_id}.png")
def track_open(tracking_id: str, db: Session = Depends(get_db)):
    message = db.query(EmailMessage).filter(EmailMessage.tracking_id == tracking_id).first()
    if message and message.opened_at is None:
        message.opened_at = datetime.utcnow()
        message.status = MessageStatus.OPENED
        lead: Lead = message.lead
        if lead.status == LeadStatus.SENT:
            lead.status = LeadStatus.OPENED
        db.commit()
    return Response(content=_PIXEL_BYTES, media_type="image/png")
