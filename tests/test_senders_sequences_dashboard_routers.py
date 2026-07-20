"""Router-level coverage for senders admin API, sequence-step config API,
and the funnel dashboard (auth gating + rendered content)."""
import pytest

from tests.conftest import make_lead_kwargs

pytestmark = pytest.mark.asyncio

ADMIN_HEADERS = {"X-API-Key": "test-admin-key"}


async def test_senders_requires_admin(client):
    resp = await client.get("/senders")
    assert resp.status_code == 401


async def test_senders_list_reflects_seeded_or_empty_pool(client):
    resp = await client.get("/senders", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_sender_pause_and_unpause_roundtrip(client, async_db):
    from app.models import SenderAccount, SenderProvider

    account = SenderAccount(
        name="pause-test-acct", provider=SenderProvider.SMTP, from_email="a@example.com",
        smtp_host="smtp.example.com", smtp_port=587,
    )
    async_db.add(account)
    await async_db.commit()
    await async_db.refresh(account)

    resp = await client.post(f"/senders/{account.id}/pause", params={"reason": "test pause"}, headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["is_paused"] is True
    assert resp.json()["pause_reason"] == "test pause"

    resp = await client.post(f"/senders/{account.id}/unpause", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["is_paused"] is False
    assert resp.json()["pause_reason"] is None


async def test_pause_unknown_sender_404(client):
    resp = await client.post("/senders/does-not-exist/pause", headers=ADMIN_HEADERS)
    assert resp.status_code == 404


async def test_sequence_steps_requires_admin(client):
    resp = await client.get("/sequence-steps")
    assert resp.status_code == 401


async def test_replace_global_sequence_steps(client):
    resp = await client.put(
        "/sequence-steps",
        json=[
            {"step_number": 1, "channel": "linkedin_view", "delay_days": 2},
            {"step_number": 2, "channel": "email", "delay_days": 3},
        ],
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200
    steps = resp.json()
    assert len(steps) == 2
    assert steps[0]["channel"] == "linkedin_view"

    resp = await client.get("/sequence-steps", headers=ADMIN_HEADERS)
    assert len(resp.json()) == 2


async def test_replace_sequence_rejects_duplicate_step_numbers(client):
    resp = await client.put(
        "/sequence-steps",
        json=[
            {"step_number": 1, "channel": "email", "delay_days": 1},
            {"step_number": 1, "channel": "linkedin_view", "delay_days": 2},
        ],
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 422


async def test_replace_sequence_for_unknown_campaign_404s(client):
    resp = await client.put(
        "/sequence-steps",
        params={"campaign_id": "nope"},
        json=[{"step_number": 1, "channel": "email", "delay_days": 1}],
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 404


async def test_dashboard_requires_admin(client):
    resp = await client.get("/dashboard")
    assert resp.status_code == 401


async def test_dashboard_renders_funnel_stats(client):
    await client.post("/leads/upload", json={"leads": [make_lead_kwargs()]})

    resp = await client.get("/dashboard", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    assert "AI SDR Funnel Dashboard" in body
    assert "Total Leads" in body
    assert "Sender Health Guardian" in body
    assert "A/B Campaign Variant Performance" in body
    assert "Pending Approval Queue" in body


async def test_dashboard_single_approval_view_404_for_unknown(client):
    resp = await client.get("/dashboard/approvals/does-not-exist", headers=ADMIN_HEADERS)
    assert resp.status_code == 404


async def test_lead_linkedin_touchpoints_endpoint(client):
    upload = await client.post("/leads/upload", json={"leads": [make_lead_kwargs()]})
    lead_id = upload.json()[0]["id"]

    resp = await client.get(f"/leads/{lead_id}/linkedin-touchpoints")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_lead_linkedin_touchpoints_unknown_lead_404(client):
    resp = await client.get("/leads/does-not-exist/linkedin-touchpoints")
    assert resp.status_code == 404
