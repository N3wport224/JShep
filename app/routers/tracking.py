"""Open tracking via a 1x1 pixel embedded in outbound HTML emails, plus a
bounce-reporting endpoint that feeds the sender-account health tracker (see
app.services.sender_rotation.record_bounce). Bounce webhooks from an SMTP
provider or Instantly/Smartlead can call POST /tracking/bounce/{message_id};
wire that URL into your provider's bounce/DSN webhook config."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_admin
from app.database import get_db
from app.models import EmailMessage, Lead, LeadStatus, MessageStatus, SenderAccount

router = APIRouter(prefix="/tracking", tags=["tracking"])

# 1x1 transparent PNG
_PIXEL_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6360000002000155e21a7e0000000049454e44ae426082"
)


@router.get("/open/{tracking_id}.png")
async def track_open(tracking_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(EmailMessage).where(EmailMessage.tracking_id == tracking_id))
    message = result.scalar_one_or_none()
    if message and message.opened_at is None:
        message.opened_at = datetime.utcnow()
        message.status = MessageStatus.OPENED
        lead = await db.get(Lead, message.lead_id)
        if lead and lead.status == LeadStatus.SENT:
            lead.status = LeadStatus.OPENED
        await db.commit()
    return Response(content=_PIXEL_BYTES, media_type="image/png")


@router.post("/bounce/{message_id}", dependencies=[Depends(require_admin)])
async def report_bounce(message_id: str, db: AsyncSession = Depends(get_db)):
    """Record a bounce for a previously-sent message. Uses the SYNCHRONOUS
    engine internally via sender_rotation's health-tracking helper is not
    possible from an async route, so this mirrors that logic directly on the
    async session to keep the web layer fully async."""
    message = await db.get(EmailMessage, message_id)
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")

    message.status = MessageStatus.BOUNCED
    if message.sender_account_id:
        account = await db.get(SenderAccount, message.sender_account_id)
        if account:
            from app.config import get_settings

            settings = get_settings()
            account.bounce_count += 1
            if (
                not account.is_paused
                and account.sent_count >= settings.bounce_rate_min_sample
                and account.bounce_rate >= settings.bounce_rate_pause_threshold
            ):
                account.is_paused = True
                account.pause_reason = (
                    f"Auto-paused: bounce rate {account.bounce_rate:.1%} exceeded "
                    f"threshold {settings.bounce_rate_pause_threshold:.1%}"
                )

    await db.commit()
    return {"message_id": message_id, "status": "bounced"}
