"""Automated lead discovery: business search / contact enrichment provider
abstractions, dedup/suppression enforcement, campaign auto-create, the
daily-cap and never-fabricate-contact-data guarantees, and Celery task
wiring into enrich_lead_task."""
from unittest.mock import MagicMock, patch

from app.config import get_settings
from app.models import Campaign, DiscoveryRun, DiscoveryRunStatus, Lead
from app.services.lead_discovery import (
    DiscoveredBusiness,
    LeadDiscoveryError,
    NullContactEnrichment,
    OwnerContact,
    parse_discovery_campaigns,
    run_discovery_for_campaign,
)
from app.services.suppression import add_to_suppression
from app.tasks.celery_tasks import discover_leads_task


def _business(name="Joe's Diner", **overrides):
    defaults = dict(name=name, address="123 Main St", website="https://joesdiner.example.com", phone="555-1234", external_id="place-1")
    defaults.update(overrides)
    return DiscoveredBusiness(**defaults)


class _FakeSearch:
    def __init__(self, businesses):
        self.businesses = businesses
        self.calls = []

    def search(self, query, count):
        self.calls.append((query, count))
        return self.businesses[:count]


class _FailingSearch:
    def search(self, query, count):
        raise LeadDiscoveryError("provider outage")


class _FakeEnrichment:
    def __init__(self, contacts_by_name):
        self.contacts_by_name = contacts_by_name
        self.calls = []

    def find_owner_contact(self, business):
        self.calls.append(business.name)
        return self.contacts_by_name.get(business.name)


# ---------------------------------------------------------------------------
# parse_discovery_campaigns
# ---------------------------------------------------------------------------


def test_parse_discovery_campaigns_default_includes_ffy_and_tip_tax_refund():
    settings = get_settings()
    configs = parse_discovery_campaigns(settings)
    names = {c["campaign_name"] for c in configs}
    assert "FFY" in names
    assert "Tip Tax Refund" in names
    for c in configs:
        assert c["daily_count"] == 25


def test_parse_discovery_campaigns_handles_invalid_json():
    from copy import copy

    settings = copy(get_settings())
    settings.lead_discovery_campaigns_json = "not json"
    assert parse_discovery_campaigns(settings) == []


# ---------------------------------------------------------------------------
# run_discovery_for_campaign
# ---------------------------------------------------------------------------


def test_run_discovery_creates_lead_with_real_contact(sync_db):
    search = _FakeSearch([_business()])
    enrichment = _FakeEnrichment({"Joe's Diner": OwnerContact(contact_name="Joe Smith", email="joe@joesdiner.example.com")})

    run, new_leads = run_discovery_for_campaign(
        sync_db, "FFY", "restaurants", daily_count=25, search_provider=search, enrichment_provider=enrichment
    )

    assert run.status == DiscoveryRunStatus.SUCCESS
    assert run.businesses_found == 1
    assert run.leads_created == 1
    assert len(new_leads) == 1
    lead = new_leads[0]
    assert lead.email == "joe@joesdiner.example.com"
    assert lead.company_name == "Joe's Diner"
    assert lead.source == "discovery"
    assert lead.campaign_id == run.campaign_id


def test_run_discovery_skips_business_with_no_contact_found(sync_db):
    search = _FakeSearch([_business()])
    run, new_leads = run_discovery_for_campaign(
        sync_db, "FFY", "restaurants", search_provider=search, enrichment_provider=NullContactEnrichment()
    )
    assert run.leads_created == 0
    assert run.no_contact_found == 1
    assert new_leads == []
    # never fabricates a lead when no real contact was found
    assert sync_db.query(Lead).filter_by(source="discovery").count() == 0


def test_run_discovery_skips_suppressed_email(sync_db):
    from app.models import SuppressionSource

    add_to_suppression(sync_db, "joe@joesdiner.example.com", SuppressionSource.MANUAL)
    search = _FakeSearch([_business()])
    enrichment = _FakeEnrichment({"Joe's Diner": OwnerContact(contact_name="Joe", email="joe@joesdiner.example.com")})

    run, new_leads = run_discovery_for_campaign(
        sync_db, "FFY", "restaurants", search_provider=search, enrichment_provider=enrichment
    )
    assert run.suppressed_skipped == 1
    assert run.leads_created == 0
    assert new_leads == []


def test_run_discovery_skips_duplicate_existing_lead(sync_db):
    sync_db.add(Lead(company_name="Existing", contact_name="Joe", email="joe@joesdiner.example.com"))
    sync_db.commit()

    search = _FakeSearch([_business()])
    enrichment = _FakeEnrichment({"Joe's Diner": OwnerContact(contact_name="Joe", email="joe@joesdiner.example.com")})

    run, new_leads = run_discovery_for_campaign(
        sync_db, "FFY", "restaurants", search_provider=search, enrichment_provider=enrichment
    )
    assert run.duplicates_skipped == 1
    assert run.leads_created == 0


def test_run_discovery_respects_daily_count_cap(sync_db):
    businesses = [_business(name=f"Biz {i}", external_id=str(i)) for i in range(40)]
    search = _FakeSearch(businesses)
    enrichment = _FakeEnrichment(
        {b.name: OwnerContact(contact_name="Owner", email=f"owner{i}@example.com") for i, b in enumerate(businesses)}
    )

    run, new_leads = run_discovery_for_campaign(
        sync_db, "FFY", "restaurants", daily_count=25, search_provider=search, enrichment_provider=enrichment
    )
    assert search.calls == [("restaurants", 25)]
    assert run.businesses_found == 25
    assert run.leads_created == 25


def test_run_discovery_auto_creates_missing_campaign_with_default_variant(sync_db):
    search = _FakeSearch([])
    run, _ = run_discovery_for_campaign(sync_db, "Brand New Campaign", "restaurants", search_provider=search, enrichment_provider=NullContactEnrichment())

    campaign = sync_db.query(Campaign).filter_by(name="Brand New Campaign").one()
    assert len(campaign.variants) == 1
    assert campaign.variants[0].label == "A"
    assert run.campaign_id == campaign.id


def test_run_discovery_reuses_existing_campaign(sync_db):
    campaign = Campaign(name="FFY")
    sync_db.add(campaign)
    sync_db.commit()

    search = _FakeSearch([])
    run, _ = run_discovery_for_campaign(sync_db, "FFY", "restaurants", search_provider=search, enrichment_provider=NullContactEnrichment())
    assert run.campaign_id == campaign.id
    assert sync_db.query(Campaign).filter_by(name="FFY").count() == 1


def test_run_discovery_records_failed_run_on_search_provider_error(sync_db):
    run, new_leads = run_discovery_for_campaign(
        sync_db, "FFY", "restaurants", search_provider=_FailingSearch(), enrichment_provider=NullContactEnrichment()
    )
    assert run.status == DiscoveryRunStatus.FAILED
    assert run.error
    assert new_leads == []
    assert sync_db.query(DiscoveryRun).filter_by(status=DiscoveryRunStatus.FAILED).count() == 1


def test_run_discovery_one_bad_enrichment_lookup_does_not_abort_run(sync_db):
    businesses = [_business(name="Good Biz", external_id="1"), _business(name="Bad Biz", external_id="2")]
    search = _FakeSearch(businesses)

    class _FlakyEnrichment:
        def find_owner_contact(self, business):
            if business.name == "Bad Biz":
                raise ConnectionError("timeout")
            return OwnerContact(contact_name="Owner", email="owner@good.example.com")

    run, new_leads = run_discovery_for_campaign(
        sync_db, "FFY", "restaurants", search_provider=search, enrichment_provider=_FlakyEnrichment()
    )
    assert run.leads_created == 1
    assert run.no_contact_found == 1


# ---------------------------------------------------------------------------
# discover_leads_task wiring
# ---------------------------------------------------------------------------


@patch("app.tasks.celery_tasks.enrich_lead_task")
@patch("app.tasks.celery_tasks.run_discovery_for_campaign")
def test_discover_leads_task_skips_when_disabled_and_not_forced(mock_run, mock_enrich, sync_db):
    settings = get_settings()
    assert settings.lead_discovery_enabled is False  # default

    result = discover_leads_task()
    assert result["status"] == "disabled"
    mock_run.assert_not_called()


@patch("app.tasks.celery_tasks.enrich_lead_task")
@patch("app.tasks.celery_tasks.run_discovery_for_campaign")
def test_discover_leads_task_runs_when_forced_even_if_disabled(mock_run, mock_enrich, sync_db):
    fake_run = MagicMock(id="run-1", businesses_found=1, leads_created=1)
    fake_run.status.value = "success"
    fake_lead = MagicMock(id="lead-1")
    mock_run.return_value = (fake_run, [fake_lead])

    result = discover_leads_task(force=True)

    assert result["status"] == "ok"
    assert mock_run.call_count == 2  # FFY + Tip Tax Refund by default
    mock_enrich.delay.assert_any_call("lead-1")


@patch("app.tasks.celery_tasks.enrich_lead_task")
@patch("app.tasks.celery_tasks.run_discovery_for_campaign")
def test_discover_leads_task_filters_to_named_campaign(mock_run, mock_enrich, sync_db):
    fake_run = MagicMock(id="run-1", businesses_found=0, leads_created=0)
    fake_run.status.value = "success"
    mock_run.return_value = (fake_run, [])

    result = discover_leads_task(campaign_name="FFY", force=True)

    assert result["status"] == "ok"
    assert mock_run.call_count == 1
    assert mock_run.call_args[0][1] == "FFY"


def test_discover_leads_task_unknown_campaign_name_returns_not_found(sync_db):
    result = discover_leads_task(campaign_name="Nonexistent Campaign", force=True)
    assert result["status"] == "not_found"
