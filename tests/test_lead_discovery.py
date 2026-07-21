"""Automated lead discovery: business search / contact enrichment provider
abstractions, dedup/suppression enforcement, campaign auto-create, the
daily-cap and never-fabricate-contact-data guarantees, and Celery task
wiring into enrich_lead_task."""
from unittest.mock import MagicMock, patch

from app.config import get_settings
from app.models import Campaign, DiscoveryRun, DiscoveryRunStatus, Lead
from app.services.lead_discovery import (
    DiscoveredBusiness,
    GooglePlacesSearch,
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
        # each vertical is covered by several precise queries, not one
        # broad combined query
        assert len(c["search_queries"]) >= 3
        assert all(isinstance(q, str) and q for q in c["search_queries"])


def test_parse_discovery_campaigns_normalizes_single_search_query_string():
    from copy import copy

    settings = copy(get_settings())
    settings.lead_discovery_campaigns_json = '[{"campaign_name":"X","search_query":"restaurants","daily_count":10}]'
    configs = parse_discovery_campaigns(settings)
    assert configs == [{"campaign_name": "X", "search_queries": ["restaurants"], "daily_count": 10}]


def test_parse_discovery_campaigns_skips_entries_with_no_queries():
    from copy import copy

    settings = copy(get_settings())
    settings.lead_discovery_campaigns_json = '[{"campaign_name":"X","daily_count":10}]'
    assert parse_discovery_campaigns(settings) == []


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
    businesses = [_business(name=f"Biz {i}", external_id=str(i), website=f"https://biz{i}.example.net") for i in range(40)]
    search = _FakeSearch(businesses)
    enrichment = _FakeEnrichment(
        {b.name: OwnerContact(contact_name="Owner", email=f"owner{i}@biz{i}.example.net") for i, b in enumerate(businesses)}
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


def test_run_discovery_rotates_through_multiple_queries_until_daily_count_met(sync_db):
    biz_a = _business(name="Biz A", external_id="a", website="https://biza.example.net")
    biz_b = _business(name="Biz B", external_id="b", website="https://bizb.example.net")

    class _MultiQuerySearch:
        def __init__(self):
            self.calls = []

        def search(self, query, count):
            self.calls.append((query, count))
            return {"bars": [biz_a], "cafes": [biz_b]}.get(query, [])

    search = _MultiQuerySearch()
    enrichment = _FakeEnrichment(
        {
            "Biz A": OwnerContact(contact_name="A", email="a@bizA.example.com"),
            "Biz B": OwnerContact(contact_name="B", email="b@bizB.example.com"),
        }
    )
    run, new_leads = run_discovery_for_campaign(
        sync_db, "FFY", ["bars", "cafes"], daily_count=25, search_provider=search, enrichment_provider=enrichment
    )
    # first query gets the full remaining budget; second only asks for what's left
    assert search.calls == [("bars", 25), ("cafes", 24)]
    assert run.businesses_found == 2
    assert run.leads_created == 2
    assert {lead.company_name for lead in new_leads} == {"Biz A", "Biz B"}


def test_run_discovery_stops_querying_once_daily_count_reached(sync_db):
    businesses = [_business(name=f"Biz {i}", external_id=str(i)) for i in range(25)]

    class _MultiQuerySearch:
        def __init__(self):
            self.calls = []

        def search(self, query, count):
            self.calls.append((query, count))
            return businesses[:count]

    search = _MultiQuerySearch()
    run, _ = run_discovery_for_campaign(
        sync_db, "FFY", ["restaurants", "bars"], daily_count=25,
        search_provider=search, enrichment_provider=NullContactEnrichment(),
    )
    # the second query is never issued - the first already filled daily_count
    assert search.calls == [("restaurants", 25)]
    assert run.businesses_found == 25


def test_run_discovery_dedups_same_business_seen_across_queries(sync_db):
    biz = _business(name="Joe's Diner", external_id="place-1")

    class _OverlappingSearch:
        def search(self, query, count):
            return [biz]

    search = _OverlappingSearch()
    enrichment = _FakeEnrichment({"Joe's Diner": OwnerContact(contact_name="Joe", email="joe@joesdiner.example.com")})
    run, new_leads = run_discovery_for_campaign(
        sync_db, "FFY", ["restaurants", "bars", "cafes"], daily_count=25,
        search_provider=search, enrichment_provider=enrichment,
    )
    assert run.businesses_found == 1  # same place ID, deduped across the 3 queries
    assert run.leads_created == 1
    assert len(new_leads) == 1


def test_run_discovery_rejects_malformed_email_as_unverified(sync_db):
    search = _FakeSearch([_business()])
    enrichment = _FakeEnrichment({"Joe's Diner": OwnerContact(contact_name="Joe", email="not-an-email")})
    run, new_leads = run_discovery_for_campaign(
        sync_db, "FFY", "restaurants", search_provider=search, enrichment_provider=enrichment
    )
    assert run.leads_created == 0
    assert run.no_contact_found == 1
    assert new_leads == []


def test_run_discovery_rejects_junk_placeholder_domain(sync_db):
    search = _FakeSearch([_business()])
    enrichment = _FakeEnrichment({"Joe's Diner": OwnerContact(contact_name="Joe", email="owner@example.com")})
    run, new_leads = run_discovery_for_campaign(
        sync_db, "FFY", "restaurants", search_provider=search, enrichment_provider=enrichment
    )
    assert run.leads_created == 0
    assert run.no_contact_found == 1
    assert new_leads == []


def test_run_discovery_skips_duplicate_website_even_with_different_email(sync_db):
    sync_db.add(Lead(company_name="Existing", contact_name="Someone", email="info@joesdiner.example.com", website="https://joesdiner.example.com"))
    sync_db.commit()

    search = _FakeSearch([_business()])
    enrichment = _FakeEnrichment({"Joe's Diner": OwnerContact(contact_name="Joe", email="joe.new@joesdiner.example.com")})
    run, new_leads = run_discovery_for_campaign(
        sync_db, "FFY", "restaurants", search_provider=search, enrichment_provider=enrichment
    )
    assert run.duplicates_skipped == 1
    assert run.leads_created == 0
    assert new_leads == []


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


# ---------------------------------------------------------------------------
# GooglePlacesSearch pagination (a single Places API page caps at 20 results,
# so satisfying daily_count=25+ requires following nextPageToken)
# ---------------------------------------------------------------------------


def _places_response(names, next_page_token=None):
    body = {
        "places": [
            {
                "displayName": {"text": name},
                "formattedAddress": "1 Main St",
                "websiteUri": f"https://{name.lower().replace(' ', '')}.example.com",
                "nationalPhoneNumber": "555-0000",
                "id": name,
            }
            for name in names
        ]
    }
    if next_page_token:
        body["nextPageToken"] = next_page_token
    return body


@patch("app.services.lead_discovery.time.sleep")
@patch("app.services.lead_discovery.httpx.post")
def test_google_places_search_pages_past_20_result_cap(mock_post, mock_sleep, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-key")
    get_settings.cache_clear()

    page1 = MagicMock(status_code=200)
    page1.json.return_value = _places_response([f"Biz {i}" for i in range(20)], next_page_token="tok-2")
    page1.raise_for_status.return_value = None
    page2 = MagicMock(status_code=200)
    page2.json.return_value = _places_response([f"Biz {i}" for i in range(20, 25)])
    page2.raise_for_status.return_value = None
    mock_post.side_effect = [page1, page2]

    results = GooglePlacesSearch().search("restaurants", 25)

    assert len(results) == 25
    assert mock_post.call_count == 2
    second_call_payload = mock_post.call_args_list[1].kwargs["json"]
    assert second_call_payload["pageToken"] == "tok-2"
    get_settings.cache_clear()


@patch("app.services.lead_discovery.httpx.post")
def test_google_places_search_stops_at_daily_count_without_extra_page(mock_post, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-key")
    get_settings.cache_clear()

    page1 = MagicMock(status_code=200)
    page1.json.return_value = _places_response([f"Biz {i}" for i in range(10)], next_page_token="tok-2")
    page1.raise_for_status.return_value = None
    mock_post.return_value = page1

    results = GooglePlacesSearch().search("restaurants", 10)

    assert len(results) == 10
    assert mock_post.call_count == 1
    get_settings.cache_clear()
