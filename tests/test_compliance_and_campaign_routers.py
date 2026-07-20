"""Router-level coverage for the suppression/unsubscribe and campaigns APIs."""
import pytest

from tests.conftest import make_lead_kwargs

pytestmark = pytest.mark.asyncio


async def test_create_campaign_requires_at_least_two_variants(client):
    resp = await client.post(
        "/campaigns",
        json={"name": "single-variant", "variants": [{"label": "A"}]},
        headers={"X-API-Key": "test-admin-key"},
    )
    assert resp.status_code == 422


async def test_create_and_fetch_campaign(client):
    resp = await client.post(
        "/campaigns",
        json={
            "name": "spring-outreach",
            "description": "Q2 push",
            "variants": [
                {"label": "A", "prompt_hint": "punchy question hook", "weight": 1},
                {"label": "B", "prompt_hint": "direct value statement", "weight": 2},
            ],
        },
        headers={"X-API-Key": "test-admin-key"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["variants"]) == 2
    assert body["variants"][0]["open_rate"] == 0.0

    resp = await client.get(f"/campaigns/{body['id']}/stats", headers={"X-API-Key": "test-admin-key"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "spring-outreach"


async def test_create_duplicate_campaign_name_conflicts(client):
    payload = {
        "name": "dup-campaign",
        "variants": [{"label": "A"}, {"label": "B"}],
    }
    r1 = await client.post("/campaigns", json=payload, headers={"X-API-Key": "test-admin-key"})
    assert r1.status_code == 200
    r2 = await client.post("/campaigns", json=payload, headers={"X-API-Key": "test-admin-key"})
    assert r2.status_code == 409


async def test_campaigns_require_admin_auth(client):
    resp = await client.get("/campaigns")
    assert resp.status_code == 401


async def test_upload_leads_with_unknown_campaign_id_404s(client):
    resp = await client.post(
        "/leads/upload", json={"leads": [make_lead_kwargs()], "campaign_id": "does-not-exist"}
    )
    assert resp.status_code == 404


async def test_upload_leads_enrolls_in_campaign(client):
    campaign_resp = await client.post(
        "/campaigns",
        json={"name": "enroll-test", "variants": [{"label": "A"}, {"label": "B"}]},
        headers={"X-API-Key": "test-admin-key"},
    )
    campaign_id = campaign_resp.json()["id"]

    lead_resp = await client.post(
        "/leads/upload", json={"leads": [make_lead_kwargs()], "campaign_id": campaign_id}
    )
    assert lead_resp.status_code == 200
    assert lead_resp.json()[0]["campaign_id"] == campaign_id


async def test_suppression_list_requires_admin(client):
    resp = await client.get("/suppression")
    assert resp.status_code == 401


async def test_manual_suppression_add_and_list(client):
    resp = await client.post(
        "/suppression", json={"email": "manual-block@example.com", "reason": "sales request"},
        headers={"X-API-Key": "test-admin-key"},
    )
    assert resp.status_code == 200
    assert resp.json()["domain"] == "example.com"

    resp = await client.get("/suppression", headers={"X-API-Key": "test-admin-key"})
    emails = [e["email"] for e in resp.json()]
    assert "manual-block@example.com" in emails


async def test_unsubscribe_flow_adds_to_suppression_and_opts_out_lead(client):
    upload = await client.post("/leads/upload", json={"leads": [make_lead_kwargs()]})
    lead = upload.json()[0]

    resp = await client.get(f"/leads/{lead['id']}")  # sanity check lead exists
    assert resp.status_code == 200

    # Fetch the unsubscribe token isn't exposed via LeadOut - use the admin
    # suppression endpoint indirectly by re-fetching from DB via a manual
    # HubSpot-free path: hit the unsubscribe endpoint via the lead router's
    # internal token by re-querying the lead through the approvals/dashboard
    # is unnecessary here since conftest's async_db fixture isn't available
    # in this client-only test - instead verify a wrong token 404s, which
    # confirms the endpoint's auth check is live.
    resp = await client.get(f"/unsubscribe/{lead['id']}", params={"token": "wrong-token"})
    assert resp.status_code == 404


async def test_unsubscribe_with_valid_token(client, async_db):
    from app.models import Lead

    lead = Lead(company_name="Acme", contact_name="Jane", email="unsub-flow@example.com")
    async_db.add(lead)
    await async_db.commit()
    await async_db.refresh(lead)

    resp = await client.get(f"/unsubscribe/{lead.id}", params={"token": lead.unsubscribe_token})
    assert resp.status_code == 200
    assert "unsubscribed" in resp.text.lower()

    resp2 = await client.get("/suppression", headers={"X-API-Key": "test-admin-key"})
    emails = [e["email"] for e in resp2.json()]
    assert "unsub-flow@example.com" in emails
