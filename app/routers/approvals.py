"""
The human-in-the-loop approval gate API. A Positive/Interested reply can
ONLY ever result in an outbound email through this endpoint, and only
after a human calls it with a valid token and an explicit 'approve' action.
Nothing in this codebase sends a reply automatically.

Race safety: the PENDING -> APPROVED/REJECTED transition happens as a single
atomic conditional UPDATE ("... WHERE id = :id AND status = 'pending'"), not
a read-then-write. Two concurrent approve/reject calls for the same request
(a double-click, or Telegram + dashboard clicked at once) can't both
succeed - a single UPDATE statement can't be interleaved with itself, so
exactly one caller's UPDATE affects a row and the other's affects zero rows
and gets a 409. This holds even on SQLite, which doesn't support real
SELECT ... FOR UPDATE row locks - the atomicity comes from the UPDATE being
one statement, not from locking.
"""
import logging
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.metrics import approval_response_latency_seconds
from app.core.security import limiter, require_admin
from app.database import get_db
from app.models import ApprovalRequest, ApprovalStatus
from app.schemas import ApprovalDecision, ApprovalRequestOut
from app.tasks.celery_tasks import send_approved_reply_task
from app.tasks.dispatch import enqueue

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.get("", response_model=list[ApprovalRequestOut], dependencies=[Depends(require_admin)])
async def list_approvals(status: ApprovalStatus | None = None, db: AsyncSession = Depends(get_db)):
    query = select(ApprovalRequest)
    if status:
        query = query.where(ApprovalRequest.status == status)
    result = await db.execute(query.order_by(ApprovalRequest.created_at.desc()))
    return result.scalars().all()


@router.get("/{approval_id}", response_model=ApprovalRequestOut, dependencies=[Depends(require_admin)])
async def get_approval(approval_id: str, db: AsyncSession = Depends(get_db)):
    approval = await db.get(ApprovalRequest, approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail="Approval request not found")
    return approval


async def resolve_decision(
    db: AsyncSession,
    approval_id: str,
    token: str,
    action: Literal["approve", "reject"],
    edited_response: str | None = None,
) -> ApprovalRequest:
    approval = await db.get(ApprovalRequest, approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail="Approval request not found")
    if approval.approval_token != token:
        raise HTTPException(status_code=403, detail="Invalid approval token")

    now = datetime.utcnow()
    new_status = ApprovalStatus.REJECTED if action == "reject" else ApprovalStatus.APPROVED
    values = {"status": new_status, "resolved_at": now, "version": ApprovalRequest.version + 1}
    if action == "approve":
        values["edited_response"] = edited_response

    result = await db.execute(
        update(ApprovalRequest)
        .where(ApprovalRequest.id == approval_id, ApprovalRequest.status == ApprovalStatus.PENDING)
        .values(**values)
    )
    await db.commit()

    if result.rowcount == 0:
        # Someone else's request already resolved it between our .get() above
        # and this UPDATE - that's the race this atomic statement prevents.
        raise HTTPException(status_code=409, detail="Approval already resolved")

    await db.refresh(approval)

    created_at = approval.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
        now_aware = datetime.now(timezone.utc)
    else:
        now_aware = datetime.now(created_at.tzinfo)
    approval_response_latency_seconds.observe((now_aware - created_at).total_seconds())

    if action == "approve":
        # Only enqueue the send after the atomic transition has committed -
        # never while a decision is still in flight.
        enqueue(send_approved_reply_task, approval.id)

    return approval


@router.get("/{approval_id}/decision", response_model=ApprovalRequestOut)
@limiter.limit("30/minute")
async def decision_via_link(
    request: Request,
    approval_id: str,
    token: str = Query(...),
    action: Literal["approve", "reject"] = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """GET variant so this can be used as a one-click link from Discord
    messages / a dashboard, e.g. the [Approve & Send] / [Reject] links.
    Authenticated purely by the per-request approval_token, not admin auth -
    this is the link a human clicks from Telegram/Discord/email."""
    return await resolve_decision(db, approval_id, token, action)


@router.post("/{approval_id}/approve", response_model=ApprovalRequestOut)
@limiter.limit("30/minute")
async def approve(
    request: Request,
    approval_id: str,
    token: str = Query(...),
    decision: ApprovalDecision = ApprovalDecision(),
    db: AsyncSession = Depends(get_db),
):
    return await resolve_decision(db, approval_id, token, "approve", edited_response=decision.edited_response)


@router.post("/{approval_id}/reject", response_model=ApprovalRequestOut)
@limiter.limit("30/minute")
async def reject(request: Request, approval_id: str, token: str = Query(...), db: AsyncSession = Depends(get_db)):
    return await resolve_decision(db, approval_id, token, "reject")
