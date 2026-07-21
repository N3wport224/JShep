"""
Automated lead discovery: find target businesses via a real, ToS-compliant
business directory API (Google Places Text Search - never raw scraping of a
site whose terms prohibit it), then hand each one to a configured contact-
enrichment provider to find a real owner/decision-maker email. A business
with no enrichment provider configured, or no contact found, is skipped
entirely rather than guessed at - this module never fabricates a person or
an email address.

Two independently pluggable stages:
  1. BusinessSearchProvider - "what businesses match this query" (Google
     Places by default).
  2. ContactEnrichmentProvider - "who's the owner/contact and what's their
     email" (bring-your-own webhook - Hunter.io, Apollo, Clearbit, an
     internal data source, whatever Jeff already has under contract).

app.tasks.celery_tasks.discover_leads_task orchestrates both stages daily
per configured campaign (see LEAD_DISCOVERY_CAMPAIGNS_JSON) and feeds new
leads straight into enrich_lead_task - the same cold-email pipeline every
other lead goes through, spam guardian included.
"""
import abc
import json
import logging
import re
import time
from dataclasses import dataclass

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.resilience import RateLimitedError, get_circuit_breaker, resilient_retry
from app.models import Campaign, CampaignVariant, DiscoveryRun, DiscoveryRunStatus, Lead
from app.services.suppression import is_suppressed

logger = logging.getLogger(__name__)


class LeadDiscoveryError(Exception):
    pass


@dataclass
class DiscoveredBusiness:
    name: str
    address: str | None
    website: str | None
    phone: str | None
    external_id: str | None  # provider's place/record ID, for audit/dedup


@dataclass
class OwnerContact:
    contact_name: str
    email: str
    linkedin_url: str | None = None


# ---------------------------------------------------------------------------
# Business search
# ---------------------------------------------------------------------------


class BusinessSearchProvider(abc.ABC):
    @abc.abstractmethod
    def search(self, query: str, count: int) -> list[DiscoveredBusiness]:
        """Return up to `count` businesses matching `query`."""


class GooglePlacesSearch(BusinessSearchProvider):
    """Google Places API (Text Search) - a legitimate, documented, rate-
    limited business directory API. Requires GOOGLE_PLACES_API_KEY.

    A single request is capped at 20 results by the API, so satisfying a
    daily_count above 20 (e.g. the default of 25) requires paging through
    `nextPageToken` - handled transparently here up to MAX_PAGES so callers
    just get up to `count` results back from one search() call."""

    api_base_url = "https://places.googleapis.com/v1/places:searchText"
    max_pages = 3  # 3 x 20 = 60 results ceiling per query, per day, per campaign
    # Google's docs note a short propagation delay is needed before a
    # nextPageToken becomes valid - this is the documented minimum.
    page_token_delay_seconds = 2

    @resilient_retry(retryable_exceptions=(RateLimitedError, ConnectionError, TimeoutError))
    def search(self, query: str, count: int) -> list[DiscoveredBusiness]:
        settings = get_settings()
        if not settings.google_places_api_key:
            raise LeadDiscoveryError("GOOGLE_PLACES_API_KEY is not configured")

        results: list[DiscoveredBusiness] = []
        page_token = None
        for page in range(self.max_pages):
            if len(results) >= count:
                break
            if page_token and self.page_token_delay_seconds:
                time.sleep(self.page_token_delay_seconds)
            batch, page_token = self._fetch_page(query, count, page_token)
            results.extend(batch)
            if not page_token:
                break
        return results[:count]

    def _fetch_page(self, query: str, count: int, page_token: str | None) -> tuple[list[DiscoveredBusiness], str | None]:
        settings = get_settings()
        breaker = get_circuit_breaker("lead_discovery:google_places")
        breaker.before_call()

        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": settings.google_places_api_key,
            "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.websiteUri,"
            "places.nationalPhoneNumber,places.id,nextPageToken",
        }
        payload = {"textQuery": query, "maxResultCount": min(count, 20)}  # Places API caps at 20/page
        if page_token:
            payload["pageToken"] = page_token

        try:
            resp = httpx.post(self.api_base_url, json=payload, headers=headers, timeout=20)
            if resp.status_code == 429:
                raise RateLimitedError(f"Google Places rate limited: {resp.text}")
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            breaker.on_failure()
            raise LeadDiscoveryError(str(exc)) from exc
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            breaker.on_failure()
            raise ConnectionError(str(exc)) from exc
        else:
            breaker.on_success()

        data = resp.json()
        places = data.get("places", [])
        businesses = [
            DiscoveredBusiness(
                name=p.get("displayName", {}).get("text", "Unknown business"),
                address=p.get("formattedAddress"),
                website=p.get("websiteUri"),
                phone=p.get("nationalPhoneNumber"),
                external_id=p.get("id"),
            )
            for p in places
        ]
        return businesses, data.get("nextPageToken")


def get_business_search_provider() -> BusinessSearchProvider:
    return GooglePlacesSearch()


# ---------------------------------------------------------------------------
# Contact enrichment
# ---------------------------------------------------------------------------


class ContactEnrichmentProvider(abc.ABC):
    @abc.abstractmethod
    def find_owner_contact(self, business: DiscoveredBusiness) -> OwnerContact | None:
        """Return the business's real owner/decision-maker contact, or None
        if nothing was found. Never fabricate a result."""


class WebhookContactEnrichment(ContactEnrichmentProvider):
    """Posts the discovered business to CONTACT_ENRICHMENT_WEBHOOK_URL and
    expects back either {} (nothing found) or
    {"contact_name": str, "email": str, "linkedin_url": str|null}. Bring
    your own provider - Hunter.io, Apollo, Clearbit, an internal database,
    whatever Jeff already has under contract for this."""

    @resilient_retry(retryable_exceptions=(RateLimitedError, ConnectionError, TimeoutError))
    def find_owner_contact(self, business: DiscoveredBusiness) -> OwnerContact | None:
        settings = get_settings()
        breaker = get_circuit_breaker("lead_discovery:contact_enrichment")
        breaker.before_call()

        headers = {}
        if settings.contact_enrichment_api_key:
            headers["Authorization"] = f"Bearer {settings.contact_enrichment_api_key}"

        payload = {
            "name": business.name,
            "address": business.address,
            "website": business.website,
            "phone": business.phone,
        }

        try:
            resp = httpx.post(settings.contact_enrichment_webhook_url, json=payload, headers=headers, timeout=20)
            if resp.status_code == 429:
                raise RateLimitedError(f"Contact enrichment webhook rate limited: {resp.text}")
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            breaker.on_failure()
            raise LeadDiscoveryError(str(exc)) from exc
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            breaker.on_failure()
            raise ConnectionError(str(exc)) from exc
        else:
            breaker.on_success()

        data = resp.json()
        email = data.get("email")
        if not email:
            return None
        return OwnerContact(
            contact_name=data.get("contact_name") or "Owner",
            email=email,
            linkedin_url=data.get("linkedin_url"),
        )


class NullContactEnrichment(ContactEnrichmentProvider):
    """No enrichment provider configured - every business is skipped rather
    than assigned a fabricated contact."""

    def find_owner_contact(self, business: DiscoveredBusiness) -> OwnerContact | None:
        return None


def get_contact_enrichment_provider() -> ContactEnrichmentProvider:
    settings = get_settings()
    if settings.contact_enrichment_webhook_url:
        return WebhookContactEnrichment()
    return NullContactEnrichment()


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def parse_discovery_campaigns(settings) -> list[dict]:
    """Parse LEAD_DISCOVERY_CAMPAIGNS_JSON into a normalized list of
    {"campaign_name", "search_queries": [...], "daily_count"} dicts. Each
    campaign may configure either a single "search_query" string (kept for
    backwards compatibility) or a "search_queries" list - multiple precise,
    vertical-specific queries (e.g. "bars and pubs", "cafes and coffee
    shops") consistently surface more relevant, on-target businesses per
    campaign than one broad combined query."""
    try:
        campaigns = json.loads(settings.lead_discovery_campaigns_json)
    except json.JSONDecodeError:
        logger.error("LEAD_DISCOVERY_CAMPAIGNS_JSON is not valid JSON; no campaigns configured for discovery")
        return []
    if not isinstance(campaigns, list):
        return []

    normalized = []
    for config in campaigns:
        if not isinstance(config, dict):
            continue
        queries = config.get("search_queries")
        if not queries and config.get("search_query"):
            queries = [config["search_query"]]
        if not isinstance(queries, list) or not queries:
            continue
        normalized.append(
            {
                "campaign_name": config.get("campaign_name"),
                "search_queries": [q for q in queries if isinstance(q, str) and q.strip()],
                "daily_count": config.get("daily_count", 25),
            }
        )
    return normalized


def _get_or_create_campaign(db: Session, name: str) -> Campaign:
    campaign = db.query(Campaign).filter(Campaign.name == name).first()
    if campaign:
        return campaign

    campaign = Campaign(name=name, description=f"Auto-created by lead discovery for {name!r}")
    db.add(campaign)
    db.flush()
    db.add(CampaignVariant(campaign_id=campaign.id, label="A", weight=1))
    db.flush()
    logger.info("Auto-created campaign %r with a default variant for lead discovery", name)
    return campaign


EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
# Free/disposable inbox providers and obvious dummy domains never belong to a
# real business owner - a contact record for one of these is unverified junk,
# not a real lead, and must not reach the campaign queue.
_JUNK_EMAIL_DOMAINS = {
    "example.com", "test.com", "domain.com", "email.com",
    "mailinator.com", "tempmail.com", "guerrillamail.com", "yopmail.com",
}


def _is_verified_contact(contact: "OwnerContact") -> bool:
    email = (contact.email or "").strip().lower()
    if not EMAIL_RE.match(email):
        return False
    domain = email.rsplit("@", 1)[-1]
    if domain in _JUNK_EMAIL_DOMAINS:
        return False
    return True


def _normalize_website(website: str | None) -> str | None:
    if not website:
        return None
    host = website.strip().lower()
    host = re.sub(r"^https?://", "", host)
    host = re.sub(r"^www\.", "", host)
    return host.rstrip("/").split("/")[0] or None


def run_discovery_for_campaign(
    db: Session,
    campaign_name: str,
    search_query: str | list[str],
    daily_count: int = 25,
    search_provider: BusinessSearchProvider | None = None,
    enrichment_provider: ContactEnrichmentProvider | None = None,
) -> tuple[DiscoveryRun, list[Lead]]:
    """Discover up to `daily_count` businesses for one campaign, enrich each
    with a real owner contact, and create a Lead for every one that yields
    real, verified contact data and isn't already a duplicate/suppressed.
    `search_query` may be a single query string or a list of queries - each
    one is searched in turn (results deduplicated by provider place ID as
    they accumulate) until `daily_count` unique businesses are collected or
    every query is exhausted, so a vertical can be covered precisely by
    several narrow queries (e.g. "bars and pubs", "cafes and coffee shops")
    instead of one broad one. Always returns (DiscoveryRun, newly-created
    leads) - never raises; the caller (app.tasks.celery_tasks.
    discover_leads_task) queues enrich_lead_task for each returned lead."""
    search_provider = search_provider or get_business_search_provider()
    enrichment_provider = enrichment_provider or get_contact_enrichment_provider()
    queries = [search_query] if isinstance(search_query, str) else list(search_query)

    campaign = _get_or_create_campaign(db, campaign_name)
    # Explicit zeroes: SQLAlchemy's column-level default=0 only applies at
    # INSERT/flush time, not at Python construction, and these fields are
    # incremented with += before this row is ever flushed.
    run = DiscoveryRun(
        campaign_id=campaign.id,
        campaign_name=campaign_name,
        search_query="; ".join(queries),
        businesses_found=0,
        leads_created=0,
        duplicates_skipped=0,
        suppressed_skipped=0,
        no_contact_found=0,
    )
    created_leads: list[Lead] = []

    businesses: list[DiscoveredBusiness] = []
    seen_external_ids: set[str] = set()
    for query in queries:
        remaining = daily_count - len(businesses)
        if remaining <= 0:
            break
        try:
            batch = search_provider.search(query, remaining)
        except Exception as exc:  # noqa: BLE001 - a provider outage must not crash the beat task
            logger.error("Lead discovery search failed for campaign %r (query %r): %s", campaign_name, query, exc)
            run.status = DiscoveryRunStatus.FAILED
            run.error = str(exc)
            db.add(run)
            db.commit()
            db.refresh(run)
            return run, created_leads
        for business in batch:
            dedup_key = business.external_id or f"{business.name}|{business.address}"
            if dedup_key in seen_external_ids:
                continue
            seen_external_ids.add(dedup_key)
            businesses.append(business)

    run.businesses_found = len(businesses)
    seen_websites_this_run: set[str] = set()

    for business in businesses:
        try:
            contact = enrichment_provider.find_owner_contact(business)
        except Exception as exc:  # noqa: BLE001 - one bad lookup must not abort the whole run
            logger.warning("Contact enrichment failed for %r: %s", business.name, exc)
            run.no_contact_found += 1
            continue

        if not contact or not _is_verified_contact(contact):
            run.no_contact_found += 1
            continue

        email = contact.email.strip().lower()
        if is_suppressed(db, email):
            run.suppressed_skipped += 1
            continue
        if db.query(Lead).filter(Lead.email.ilike(email)).first():
            run.duplicates_skipped += 1
            continue

        website_key = _normalize_website(business.website)
        if website_key:
            if website_key in seen_websites_this_run:
                run.duplicates_skipped += 1
                continue
            if db.query(Lead).filter(Lead.website.ilike(f"%{website_key}%")).first():
                run.duplicates_skipped += 1
                continue
            seen_websites_this_run.add(website_key)

        lead = Lead(
            company_name=business.name,
            contact_name=contact.contact_name,
            website=business.website,
            email=email,
            linkedin_url=contact.linkedin_url,
            campaign_id=campaign.id,
            source="discovery",
        )
        db.add(lead)
        db.flush()
        created_leads.append(lead)
        run.leads_created += 1

    db.add(run)
    db.commit()
    db.refresh(run)
    for lead in created_leads:
        db.refresh(lead)
    logger.info(
        "Discovery run for campaign %r: %d found, %d created, %d duplicates, %d suppressed, %d no-contact",
        campaign_name, run.businesses_found, run.leads_created, run.duplicates_skipped,
        run.suppressed_skipped, run.no_contact_found,
    )
    return run, created_leads
