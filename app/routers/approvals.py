"""
The human-in-the-loop approval gate API. A Positive/Interested reply can
ONLY ever result in an outbound email through this endpoint, and only
after a human calls it with a valid token and an explicit 'approve' action.
Nothing in this codebase sends a reply automatically.
"""
import logging
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ApprovalRequest, ApprovalStatus, EmailMessage, MessageDirection, MessageStatus
from app.schemas import ApprovalDecision, ApprovalRequestOut
from app.services.email_sender import EmailSendError, send_email

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.get("", response_model=list[ApprovalRequestOut])
def list_approvals(status: ApprovalStatus | None = None, db: Session = Depends(get_db)):
    query = db.query(ApprovalRequest)
    if status:
        query = query.filter(ApprovalRequest.status == status)
    return query.order_by(ApprovalRequest.created_at.desc()).all()


@router.get("/{approval_id}", response_model=ApprovalRequestOut)
def get_approval(approval_id: str, db: Session = Depends(get_db)):
    approval = db.get(ApprovalRequest, approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail="Approval request not found")
    return approval


def _execute_decision(
    db: Session,
    approval: ApprovalRequest,
    token: str,
    action: Literal["approve", "reject"],
    edited_response: str | None = None,
) -> ApprovalRequest:
    if approval.approval_token != token:
        raise HTTPException(status_code=403, detail="Invalid approval token")
    if approval.status != ApprovalStatus.PENDING:
        raise HTTPException(status_code=409, detail=f"Approval already resolved as {approval.status}")

    if action == "reject":
        approval.status = ApprovalStatus.REJECTED
        approval.resolved_at = datetime.utcnow()
        db.commit()
        db.refresh(approval)
        return approval

    # action == "approve"
    final_body = edited_response or approval.drafted_response
    approval.edited_response = edited_response
    approval.status = ApprovalStatus.APPROVED

    lead = approval.lead
    reply = approval.reply
    last_outbound = (
        db.query(EmailMessage)
        .filter(EmailMessage.lead_id == lead.id, EmailMessage.direction == MessageDirection.OUTBOUND)
        .order_by(EmailMessage.created_at.desc())
        .first()
    )
    subject = f"Re: {reply.raw_subject}" if reply.raw_subject else "Re: your reply"

    message = EmailMessage(
        lead_id=lead.id,
        direction=MessageDirection.OUTBOUND,
        subject=subject,
        body=final_body,
        status=MessageStatus.DRAFT,
    )
    db.add(message)
    db.flush()

    try:
        message_id_header = send_email(
            to_email=lead.email,
            subject=subject,
            body_text=final_body,
            tracking_id=message.tracking_id,
            in_reply_to=last_outbound.message_id_header if last_outbound else None,
        )
    except EmailSendError as exc:
        message.status = MessageStatus.FAILED
        db.commit()
        raise HTTPException(status_code=502, detail=f"Approved but failed to send reply: {exc}") from exc

    message.status = MessageStatus.SENT
    message.message_id_header = message_id_header
    message.sent_at = datetime.utcnow()
    approval.status = ApprovalStatus.SENT
    approval.resolved_at = datetime.utcnow()
    db.commit()
    db.refresh(approval)
    return approval


@router.get("/{approval_id}/decision", response_model=ApprovalRequestOut)
def decision_via_link(
    approval_id: str,
    token: str = Query(...),
    action: Literal["approve", "reject"] = Query(...),
    db: Session = Depends(get_db),
):
    """GET variant so this can be used as a one-click link from Discord
    messages / a dashboard, e.g. the [Approve & Send] / [Reject] links."""
    approval = db.get(ApprovalRequest, approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail="Approval request not found")
    return _execute_decision(db, approval, token, action)


@router.post("/{approval_id}/approve", response_model=ApprovalRequestOut)
def approve(
    approval_id: str,
    token: str = Query(...),
    decision: ApprovalDecision = ApprovalDecision(),
    db: Session = Depends(get_db),
):
    approval = db.get(ApprovalRequest, approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail="Approval request not found")
    return _execute_decision(db, approval, token, "approve", edited_response=decision.edited_response)


@router.post("/{approval_id}/reject", response_model=ApprovalRequestOut)
def reject(approval_id: str, token: str = Query(...), db: Session = Depends(get_db)):
    approval = db.get(ApprovalRequest, approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail="Approval request not found")
    return _execute_decision(db, approval, token, "reject")
