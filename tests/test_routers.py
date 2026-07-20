"""Async router smoke tests: lead ingestion, auth gating on admin routes,
and that enrichment/sending only ever enqueue work rather than doing it
inline on the request path."""
from unittest.mock import patch

import pytest

from tests.conftest import make_lead_kwargs

pytestmark = pytest.mark.asyncio


async def test_upload_leads_json(client):
    resp = await client.post("/leads/upload", json={"leads": [make_lead_kwargs()]})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["status"] == "new"


async def test_upload_leads_dedupes_by_email(client):
    lead = make_lead_kwargs()
    resp1 = await client.post("/leads/upload", json={"leads": [lead]})
    resp2 = await client.post("/leads/upload", json={"leads": [lead]})
    assert len(resp1.json()) == 1
    assert len(resp2.json()) == 0  # already exists, silently skipped


async def test_upload_leads_csv_file(client):
    csv_content = "company_name,contact_name,email\nBeta Inc,Bob,bob-test@beta.example.com\n"
    resp = await client.post(
        "/leads/upload/file", files={"file": ("leads.csv", csv_content, "text/csv")}
    )
    assert resp.status_code == 200
    assert resp.json()[0]["company_name"] == "Beta Inc"


async def test_upload_leads_csv_missing_columns_rejected(client):
    resp = await client.post(
        "/leads/upload/file", files={"file": ("leads.csv", "name,email\nBob,bob@x.com\n", "text/csv")}
    )
    assert resp.status_code == 422


@patch("app.routers.leads.enqueue", return_value="task-123")
async def test_enrich_lead_queues_task_not_inline(mock_enqueue, client):
    upload = await client.post("/leads/upload", json={"leads": [make_lead_kwargs()]})
    lead_id = upload.json()[0]["id"]

    resp = await client.post(f"/leads/{lead_id}/enrich")
    assert resp.status_code == 202
    assert resp.json()["task_id"] == "task-123"
    mock_enqueue.assert_called_once()


async def test_enrich_unknown_lead_404(client):
    resp = await client.post("/leads/does-not-exist/enrich")
    assert resp.status_code == 404


async def test_approvals_list_requires_admin_auth(client):
    resp = await client.get("/approvals")
    assert resp.status_code == 401


async def test_approvals_list_with_api_key_succeeds(client):
    resp = await client.get("/approvals", headers={"X-API-Key": "test-admin-key"})
    assert resp.status_code == 200


async def test_dashboard_requires_admin_auth(client):
    resp = await client.get("/dashboard")
    assert resp.status_code == 401


@patch("app.routers.inbound.enqueue", return_value="task-456")
async def test_inbound_webhook_unknown_lead_404(mock_enqueue, client):
    resp = await client.post(
        "/inbound/webhook", json={"lead_email": "nobody@nowhere.example.com", "body": "hi"}
    )
    assert resp.status_code == 404
    mock_enqueue.assert_not_called()


@patch("app.routers.inbound.enqueue", return_value="task-456")
async def test_inbound_webhook_known_lead_queues_triage(mock_enqueue, client):
    upload = await client.post("/leads/upload", json={"leads": [make_lead_kwargs(email="known@acme.example.com")]})
    assert upload.status_code == 200

    resp = await client.post(
        "/inbound/webhook", json={"lead_email": "known@acme.example.com", "body": "Interested, let's chat"}
    )
    assert resp.status_code == 202
    assert resp.json()["sentiment"] is None  # triage hasn't run yet - it's queued, not inline
    mock_enqueue.assert_called_once()
