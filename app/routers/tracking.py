"""
Open tracking via a 1x1 pixel embedded in outbound HTML emails, plus
bounce/spam-complaint reporting endpoints that feed the sender-account
health guardian (see app.services.sender_rotation - the pause logic here
is a hand-written async mirror of _maybe_auto_pause since that helper uses
the sync engine, which isn't usable from an async route). Wire your SMTP
provider's bounce/DSN webhook to POST /tracking/bounce/{message_id}, and
your mailbox provider's feedback-loop webhook to
POST /tracking/spam-complaint/{message_id}.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.metrics import sender_bounce_rate, sender_spam_complaint_rate, variant_open_total
from app.core.security import require_admin
from app.database import get_db
from app.models import CampaignVariant, EmailMessage, Lead, LeadStatus, MessageStatus, SenderAccount

router = APIRouter(prefix="/tracking", tags=["tracking"])

# 1x1 transparent PNG
_PIXEL_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6360000002000155e21a7e0000000049454e44ae426082"
)


def _maybe_auto_pause(account: SenderAccount) -> None:
    """Async-route mirror of app.services.sender_rotation._maybe_auto_pause -
    kept in sync with the same threshold semantics; see that function for
    the full rationale."""
    settings = get_settings()
    if account.is_paused or account.sent_count < settings.bounce_rate_min_sample:
        return
    if account.bounce_rate >= settings.bounce_rate_pause_threshold:
        account.is_paused = True
        account.pause_reason = (
            f"Auto-paused: bounce rate {account.bounce_rate:.1%} exceeded "
            f"threshold {settings.bounce_rate_pause_threshold:.1%}"
        )
    elif account.spam_complaint_rate >= settings.spam_complaint_rate_pause_threshold:
        account.is_paused = True
        account.pause_reason = (
            f"Auto-paused: spam complaint rate {account.spam_complaint_rate:.2%} exceeded "
            f"threshold {settings.spam_complaint_rate_pause_threshold:.2%}"
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
        if lead and lead.variant_id:
            variant = await db.get(CampaignVariant, lead.variant_id)
            if variant:
                variant.open_count += 1
                variant_open_total.labels(campaign=variant.campaign.name, variant=variant.label).inc()
        if message.sender_account_id:
            account = await db.get(SenderAccount, message.sender_account_id)
            if account:
                account.open_count += 1
        await db.commit()
    return Response(content=_PIXEL_BYTES, media_type="image/png")


@router.post("/bounce/{message_id}", dependencies=[Depends(require_admin)])
async def report_bounce(message_id: str, db: AsyncSession = Depends(get_db)):
    """Record a bounce for a previously-sent message and update its sender
    account's health tracking (may auto-pause the account)."""
    message = await db.get(EmailMessage, message_id)
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")

    message.status = MessageStatus.BOUNCED
    if message.sender_account_id:
        account = await db.get(SenderAccount, message.sender_account_id)
        if account:
            account.bounce_count += 1
            sender_bounce_rate.labels(sender_account=account.name).observe(account.bounce_rate)
            _maybe_auto_pause(account)

    await db.commit()
    return {"message_id": message_id, "status": "bounced"}


@router.post("/spam-complaint/{message_id}", dependencies=[Depends(require_admin)])
async def report_spam_complaint(message_id: str, db: AsyncSession = Depends(get_db)):
    """Record a spam complaint (mailbox provider feedback loop) for a
    previously-sent message and update its sender account's health
    tracking. Spam complaints use a much stricter auto-pause threshold
    (default 0.1%) than bounces (default 2%)."""
    message = await db.get(EmailMessage, message_id)
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")

    if message.sender_account_id:
        account = await db.get(SenderAccount, message.sender_account_id)
        if account:
            account.spam_complaint_count += 1
            sender_spam_complaint_rate.labels(sender_account=account.name).observe(account.spam_complaint_rate)
            _maybe_auto_pause(account)

    await db.commit()
    return {"message_id": message_id, "status": "spam_complaint_recorded"}
