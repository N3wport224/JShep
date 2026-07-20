"""Router coverage for the spam-guardian review queue, plus lock-in checks
for the core email engine (templating merge, IMAP/webhook -> thread memory
-> sentiment trigger)."""
import pytest

from tests.conftest import make_lead_kwargs

pytestmark = pytest.mark.asyncio

ADMIN_HEADERS = {"X-API-Key": "test-admin-key"}


# ---------------------------------------------------------------------------
# Outbound review queue
# ---------------------------------------------------------------------------


async def test_send_rejects_needs_review_message(client, async_db):
    from app.models import EmailMessage, Lead, MessageStatus

    lead = Lead(company_name="Acme", contact_name="Jane", email="needs-review@example.com")
    async_db.add(lead)
    await async_db.flush()
    message = EmailMessage(
        lead_id=lead.id, subject="ACT NOW", body="spammy copy", status=MessageStatus.NEEDS_REVIEW,
        spam_flagged=True, spam_score=85,
    )
    async_db.add(message)
    await async_db.commit()
    await async_db.refresh(message)

    resp = await client.post(f"/outbound/send/{message.id}")
    assert resp.status_code == 409


async def test_approve_review_requires_admin(client, async_db):
    from app.models import EmailMessage, Lead, MessageStatus

    lead = Lead(company_name="Acme", contact_name="Jane", email="needs-review2@example.com")
    async_db.add(lead)
    await async_db.flush()
    message = EmailMessage(
        lead_id=lead.id, subject="ACT NOW", body="spammy copy", status=MessageStatus.NEEDS_REVIEW,
        spam_flagged=True, spam_score=85,
    )
    async_db.add(message)
    await async_db.commit()
    await async_db.refresh(message)

    resp = await client.post(f"/outbound/messages/{message.id}/approve-review", json={})
    assert resp.status_code == 401


async def test_approve_review_clears_message_to_draft_and_allows_send(client, async_db):
    from unittest.mock import patch

    from app.models import EmailMessage, Lead, MessageStatus

    lead = Lead(company_name="Acme", contact_name="Jane", email="needs-review3@example.com")
    async_db.add(lead)
    await async_db.flush()
    message = EmailMessage(
        lead_id=lead.id, subject="ACT NOW", body="spammy copy", status=MessageStatus.NEEDS_REVIEW,
        spam_flagged=True, spam_score=85, spam_reasons="trigger words",
    )
    async_db.add(message)
    await async_db.commit()
    await async_db.refresh(message)

    resp = await client.post(
        f"/outbound/messages/{message.id}/approve-review",
        json={"subject": "A better subject", "body": "A calmer, rewritten body."},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "draft"
    assert body["subject"] == "A better subject"
    assert body["body"] == "A calmer, rewritten body."

    with patch("app.routers.outbound.enqueue", return_value="task-1") as mock_enqueue:
        resp = await client.post(f"/outbound/send/{message.id}")
        assert resp.status_code == 202
        mock_enqueue.assert_called_once()


async def test_approve_review_rejects_message_not_pending_review(client, async_db):
    from app.models import EmailMessage, Lead, MessageStatus

    lead = Lead(company_name="Acme", contact_name="Jane", email="already-draft@example.com")
    async_db.add(lead)
    await async_db.flush()
    message = EmailMessage(lead_id=lead.id, subject="Hi", body="normal copy", status=MessageStatus.DRAFT)
    async_db.add(message)
    await async_db.commit()
    await async_db.refresh(message)

    resp = await client.post(
        f"/outbound/messages/{message.id}/approve-review", json={}, headers=ADMIN_HEADERS
    )
    assert resp.status_code == 409


async def test_list_messages_filters_by_status(client, async_db):
    from app.models import EmailMessage, Lead, MessageStatus

    lead = Lead(company_name="Acme", contact_name="Jane", email="filter-test@example.com")
    async_db.add(lead)
    await async_db.flush()
    async_db.add(EmailMessage(lead_id=lead.id, subject="A", body="a", status=MessageStatus.DRAFT))
    async_db.add(
        EmailMessage(
            lead_id=lead.id, subject="B", body="b", status=MessageStatus.NEEDS_REVIEW, spam_flagged=True, spam_score=90
        )
    )
    await async_db.commit()

    resp = await client.get("/outbound/messages", params={"status": "needs_review"})
    assert resp.status_code == 200
    results = resp.json()
    assert len(results) == 1
    assert results[0]["status"] == "needs_review"
    assert results[0]["spam_flagged"] is True


async def test_dashboard_shows_spam_review_queue_section(client):
    resp = await client.get("/dashboard", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    assert "Spam Guardian Review Queue" in resp.text
    assert "Spam-Flagged" in resp.text


# ---------------------------------------------------------------------------
# Core email lifecycle lock-in: templating merge, IMAP -> thread memory ->
# sentiment trigger (already covered end to end in test_sentiment_triage.py
# / test_multichannel_sequence_task.py - these confirm the pieces the spam
# guardian sits in front of are unaffected).
# ---------------------------------------------------------------------------


async def test_upload_and_list_leads_round_trip(client):
    resp = await client.post("/leads/upload", json={"leads": [make_lead_kwargs()]})
    assert resp.status_code == 200
    lead = resp.json()[0]

    resp = await client.get(f"/leads/{lead['id']}")
    assert resp.status_code == 200
    assert resp.json()["email"] == lead["email"]


async def test_inbound_webhook_still_queues_triage_after_spam_guardian_changes(client):
    from unittest.mock import patch

    upload = await client.post("/leads/upload", json={"leads": [make_lead_kwargs(email="webhook-check@acme.example.com")]})
    assert upload.status_code == 200

    with patch("app.routers.inbound.enqueue", return_value="task-x") as mock_enqueue:
        resp = await client.post(
            "/inbound/webhook", json={"lead_email": "webhook-check@acme.example.com", "body": "Interested!"}
        )
        assert resp.status_code == 202
        mock_enqueue.assert_called_once()
