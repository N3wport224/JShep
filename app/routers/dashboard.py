"""
Real-time funnel & operations dashboard: lead funnel counts, delivery/open/
reply/bounce rates, sender health guardian status, A/B campaign variant
comparison, and the pending human-in-the-loop approval queue with inline
Approve/Reject actions. Server-rendered (Jinja2, no JS framework) with a
short auto-refresh for "real-time" without needing websockets.

Protected by the same admin auth as other administrative routes (API key
or JWT) - pass ?token=<jwt> if opening directly in a browser without a way
to set headers.
"""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_admin
from app.database import get_db
from app.models import (
    ApprovalRequest,
    ApprovalStatus,
    Campaign,
    EmailMessage,
    Lead,
    LeadStatus,
    LinkedInTouchpoint,
    MessageDirection,
    MessageStatus,
    Reply,
    SenderAccount,
)

router = APIRouter(prefix="/dashboard", tags=["dashboard"], dependencies=[Depends(require_admin)])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _safe_rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


async def gather_funnel_stats(db: AsyncSession) -> dict:
    total_leads = (await db.execute(select(func.count()).select_from(Lead))).scalar_one()

    status_rows = (await db.execute(select(Lead.status, func.count()).group_by(Lead.status))).all()
    leads_by_status = {status.value: count for status, count in status_rows}
    for status in LeadStatus:
        leads_by_status.setdefault(status.value, 0)

    messages_sent = (
        await db.execute(
            select(func.count()).select_from(EmailMessage).where(
                EmailMessage.direction == MessageDirection.OUTBOUND, EmailMessage.sent_at.isnot(None)
            )
        )
    ).scalar_one()
    messages_opened = (
        await db.execute(
            select(func.count()).select_from(EmailMessage).where(
                EmailMessage.direction == MessageDirection.OUTBOUND, EmailMessage.opened_at.isnot(None)
            )
        )
    ).scalar_one()
    messages_bounced = (
        await db.execute(
            select(func.count()).select_from(EmailMessage).where(
                EmailMessage.direction == MessageDirection.OUTBOUND, EmailMessage.status == MessageStatus.BOUNCED
            )
        )
    ).scalar_one()
    messages_needs_review = (
        await db.execute(
            select(func.count()).select_from(EmailMessage).where(
                EmailMessage.direction == MessageDirection.OUTBOUND,
                EmailMessage.status == MessageStatus.NEEDS_REVIEW,
            )
        )
    ).scalar_one()

    replies_received = (await db.execute(select(func.count()).select_from(Reply))).scalar_one()

    approval_rows = (
        await db.execute(select(ApprovalRequest.status, func.count()).group_by(ApprovalRequest.status))
    ).all()
    approvals_by_status = {status.value: count for status, count in approval_rows}
    for status in ApprovalStatus:
        approvals_by_status.setdefault(status.value, 0)

    touchpoint_rows = (
        await db.execute(select(LinkedInTouchpoint.status, func.count()).group_by(LinkedInTouchpoint.status))
    ).all()
    touchpoints_by_status = {status.value: count for status, count in touchpoint_rows}

    enriched_or_further = total_leads - leads_by_status.get(LeadStatus.NEW.value, 0)

    return {
        "total_leads": total_leads,
        "leads_by_status": leads_by_status,
        "enrichment_rate": _safe_rate(enriched_or_further, total_leads),
        "messages_sent": messages_sent,
        "messages_opened": messages_opened,
        "messages_bounced": messages_bounced,
        "messages_needs_review": messages_needs_review,
        "replies_received": replies_received,
        "open_rate": _safe_rate(messages_opened, messages_sent),
        "reply_rate": _safe_rate(replies_received, messages_sent),
        "bounce_rate": _safe_rate(messages_bounced, messages_sent),
        "approvals_by_status": approvals_by_status,
        "touchpoints_by_status": touchpoints_by_status,
    }


@router.get("", response_class=HTMLResponse)
async def dashboard_home(request: Request, db: AsyncSession = Depends(get_db)):
    stats = await gather_funnel_stats(db)

    senders = (await db.execute(select(SenderAccount).order_by(SenderAccount.name))).scalars().all()
    campaigns = (await db.execute(select(Campaign).order_by(Campaign.created_at.desc()))).scalars().all()
    pending_approvals = (
        (
            await db.execute(
                select(ApprovalRequest)
                .where(ApprovalRequest.status == ApprovalStatus.PENDING)
                .order_by(ApprovalRequest.created_at.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    flagged_messages = (
        (
            await db.execute(
                select(EmailMessage)
                .where(EmailMessage.status == MessageStatus.NEEDS_REVIEW)
                .order_by(EmailMessage.created_at.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )

    return templates.TemplateResponse(
        "dashboard_home.html",
        {
            "request": request,
            "stats": stats,
            "senders": senders,
            "campaigns": campaigns,
            "approvals": pending_approvals,
            "flagged_messages": flagged_messages,
        },
    )


@router.get("/approvals/{approval_id}", response_class=HTMLResponse)
async def dashboard_single(approval_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    approval = await db.get(ApprovalRequest, approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail="Approval request not found")
    return templates.TemplateResponse("dashboard_list.html", {"request": request, "approvals": [approval]})
