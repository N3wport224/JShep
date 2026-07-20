"""
Race-condition coverage for the human-in-the-loop approval gate: two
concurrent decision calls for the same ApprovalRequest must not both
succeed. See app.routers.approvals.resolve_decision - the PENDING ->
resolved transition is a single atomic conditional UPDATE, not a
read-then-write, so this holds even without real database row locks
(which SQLite, used here, doesn't support).
"""
import asyncio
import uuid

import pytest
from unittest.mock import patch

from app.models import ApprovalRequest, Lead, Reply
from app.routers.approvals import resolve_decision
from app.database import AsyncSessionLocal
from fastapi import HTTPException

pytestmark = pytest.mark.asyncio


async def _make_pending_approval(async_db) -> ApprovalRequest:
    lead = Lead(company_name="Acme", contact_name="Jane", email=f"jane-{uuid.uuid4().hex[:6]}@acme.example.com")
    async_db.add(lead)
    await async_db.flush()

    reply = Reply(lead_id=lead.id, raw_subject="Re: intro", raw_body="Sounds great, let's talk!")
    async_db.add(reply)
    await async_db.flush()

    approval = ApprovalRequest(lead_id=lead.id, reply_id=reply.id, drafted_response="Happy to chat, how's Tuesday?")
    async_db.add(approval)
    await async_db.commit()
    await async_db.refresh(approval)
    return approval


@patch("app.routers.approvals.enqueue", return_value="fake-task-id")
async def test_double_approve_only_one_succeeds(mock_enqueue, async_db):
    approval = await _make_pending_approval(async_db)

    # Two independent sessions, like two concurrent HTTP requests would get.
    async def attempt():
        async with AsyncSessionLocal() as session:
            try:
                result = await resolve_decision(session, approval.id, approval.approval_token, "approve")
                return ("ok", result.status)
            except HTTPException as exc:
                return ("error", exc.status_code)

    results = await asyncio.gather(attempt(), attempt())
    outcomes = [r[0] for r in results]
    assert outcomes.count("ok") == 1
    assert outcomes.count("error") == 1
    error_result = next(r for r in results if r[0] == "error")
    assert error_result[1] == 409

    # Send should only ever have been enqueued once, not twice.
    assert mock_enqueue.call_count == 1


@patch("app.routers.approvals.enqueue", return_value="fake-task-id")
async def test_approve_then_reject_second_call_is_409(mock_enqueue, async_db):
    approval = await _make_pending_approval(async_db)

    async with AsyncSessionLocal() as s1:
        result = await resolve_decision(s1, approval.id, approval.approval_token, "approve")
        assert result.status.value == "approved"

    async with AsyncSessionLocal() as s2:
        with pytest.raises(HTTPException) as exc_info:
            await resolve_decision(s2, approval.id, approval.approval_token, "reject")
        assert exc_info.value.status_code == 409


async def test_wrong_token_is_403(async_db):
    approval = await _make_pending_approval(async_db)
    async with AsyncSessionLocal() as session:
        with pytest.raises(HTTPException) as exc_info:
            await resolve_decision(session, approval.id, "wrong-token", "approve")
        assert exc_info.value.status_code == 403


async def test_unknown_approval_id_is_404(async_db):
    async with AsyncSessionLocal() as session:
        with pytest.raises(HTTPException) as exc_info:
            await resolve_decision(session, "does-not-exist", "any-token", "approve")
        assert exc_info.value.status_code == 404
