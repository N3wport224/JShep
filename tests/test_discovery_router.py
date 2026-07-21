"""Router coverage for the lead discovery admin API."""
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.asyncio

ADMIN_HEADERS = {"X-API-Key": "test-admin-key"}


async def test_discovery_run_requires_admin(client):
    resp = await client.post("/discovery/run", json={})
    assert resp.status_code == 401


async def test_discovery_runs_list_requires_admin(client):
    resp = await client.get("/discovery/runs")
    assert resp.status_code == 401


@patch("app.routers.discovery.enqueue", return_value="task-123")
async def test_trigger_discovery_queues_task_forced(mock_enqueue, client):
    resp = await client.post("/discovery/run", json={}, headers=ADMIN_HEADERS)
    assert resp.status_code == 202
    assert resp.json()["task_id"] == "task-123"
    args, kwargs = mock_enqueue.call_args
    assert kwargs.get("force") is True


@patch("app.routers.discovery.enqueue", return_value="task-456")
async def test_trigger_discovery_with_campaign_name(mock_enqueue, client):
    resp = await client.post("/discovery/run", json={"campaign_name": "FFY"}, headers=ADMIN_HEADERS)
    assert resp.status_code == 202
    args, kwargs = mock_enqueue.call_args
    assert "FFY" in args


async def test_list_discovery_runs_empty(client):
    resp = await client.get("/discovery/runs", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_discovery_runs_returns_created_run(client, async_db):
    from app.models import Campaign, DiscoveryRun

    campaign = Campaign(name="FFY")
    async_db.add(campaign)
    await async_db.flush()
    run = DiscoveryRun(
        campaign_id=campaign.id, campaign_name="FFY", search_query="restaurants",
        businesses_found=25, leads_created=10,
    )
    async_db.add(run)
    await async_db.commit()

    resp = await client.get("/discovery/runs", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    results = resp.json()
    assert len(results) == 1
    assert results[0]["campaign_name"] == "FFY"
    assert results[0]["leads_created"] == 10


async def test_list_discovery_runs_filters_by_campaign_name(client, async_db):
    from app.models import DiscoveryRun

    async_db.add(DiscoveryRun(campaign_name="FFY", search_query="q1", businesses_found=1))
    async_db.add(DiscoveryRun(campaign_name="Tip Tax Refund", search_query="q2", businesses_found=2))
    await async_db.commit()

    resp = await client.get("/discovery/runs", params={"campaign_name": "FFY"}, headers=ADMIN_HEADERS)
    results = resp.json()
    assert len(results) == 1
    assert results[0]["campaign_name"] == "FFY"
