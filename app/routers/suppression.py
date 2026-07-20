"""
Global suppression/do-not-contact list. Admin routes manage entries
directly; GET /unsubscribe/{lead_id} is the public, token-protected
one-click link included in the footer of every cold email and follow-up
(see app.services.suppression.unsubscribe_footer) - clicking it adds both
the lead's email and domain to the suppression list immediately.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.metrics import suppression_total
from app.core.security import limiter, require_admin
from app.database import get_db
from app.models import Lead, LeadStatus, SuppressionEntry, SuppressionSource
from app.schemas import SuppressionEntryIn, SuppressionEntryOut

logger = logging.getLogger(__name__)
router = APIRouter(tags=["suppression"])


@router.get("/suppression", response_model=list[SuppressionEntryOut], dependencies=[Depends(require_admin)])
async def list_suppression(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(SuppressionEntry).order_by(SuppressionEntry.created_at.desc()))
    return result.scalars().all()


@router.post("/suppression", response_model=SuppressionEntryOut, dependencies=[Depends(require_admin)])
async def add_suppression(payload: SuppressionEntryIn, db: AsyncSession = Depends(get_db)):
    email = payload.email.strip().lower()
    domain = email.rsplit("@", 1)[-1]

    existing = (await db.execute(select(SuppressionEntry).where(SuppressionEntry.email == email))).scalar_one_or_none()
    if existing:
        return existing

    entry = SuppressionEntry(email=email, domain=domain, reason=payload.reason, source=SuppressionSource.MANUAL)
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    suppression_total.labels(source=SuppressionSource.MANUAL.value).inc()
    return entry


@router.delete("/suppression/{entry_id}", dependencies=[Depends(require_admin)])
async def remove_suppression(entry_id: str, db: AsyncSession = Depends(get_db)):
    entry = await db.get(SuppressionEntry, entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Suppression entry not found")
    await db.delete(entry)
    await db.commit()
    return {"id": entry_id, "status": "removed"}


@router.get("/unsubscribe/{lead_id}", response_class=HTMLResponse)
@limiter.limit("30/minute")
async def unsubscribe(request: Request, lead_id: str, token: str = Query(...), db: AsyncSession = Depends(get_db)):
    lead = await db.get(Lead, lead_id)
    if not lead or lead.unsubscribe_token != token:
        raise HTTPException(status_code=404, detail="Invalid unsubscribe link")

    email = lead.email.strip().lower()
    domain = email.rsplit("@", 1)[-1]
    existing = (await db.execute(select(SuppressionEntry).where(SuppressionEntry.email == email))).scalar_one_or_none()
    if not existing:
        db.add(SuppressionEntry(email=email, domain=domain, source=SuppressionSource.UNSUBSCRIBE_LINK))
        suppression_total.labels(source=SuppressionSource.UNSUBSCRIBE_LINK.value).inc()

    lead.status = LeadStatus.OPTED_OUT
    lead.next_follow_up_at = None
    await db.commit()

    return (
        "<!doctype html><html><body style='font-family:system-ui;max-width:480px;margin:4rem auto;text-align:center'>"
        "<h2>You've been unsubscribed</h2>"
        f"<p>{email} will not receive any further emails from us.</p>"
        "</body></html>"
    )
