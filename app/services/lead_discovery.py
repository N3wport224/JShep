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
    limited business directory API. Requires GOOGLE_PLACES_API_KEY."""

    api_base_url = "https://places.googleapis.com/v1/places:searchText"

    @resilient_retry(retryable_exceptions=(RateLimitedError, ConnectionError, TimeoutError))
    def search(self, query: str, count: int) -> list[DiscoveredBusiness]:
        settings = get_settings()
        if not settings.google_places_api_key:
            raise LeadDiscoveryError("GOOGLE_PLACES_API_KEY is not configured")

        breaker = get_circuit_breaker("lead_discovery:google_places")
        breaker.before_call()

        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": settings.google_places_api_key,
            "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.websiteUri,"
            "places.nationalPhoneNumber,places.id",
        }
        payload = {"textQuery": query, "maxResultCount": min(count, 20)}  # Places API caps at 20/page

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

        places = resp.json().get("places", [])
        return [
            DiscoveredBusiness(
                name=p.get("displayName", {}).get("text", "Unknown business"),
                address=p.get("formattedAddress"),
                website=p.get("websiteUri"),
                phone=p.get("nationalPhoneNumber"),
                external_id=p.get("id"),
            )
            for p in places[:count]
        ]


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
    try:
        campaigns = json.loads(settings.lead_discovery_campaigns_json)
    except json.JSONDecodeError:
        logger.error("LEAD_DISCOVERY_CAMPAIGNS_JSON is not valid JSON; no campaigns configured for discovery")
        return []
    return campaigns if isinstance(campaigns, list) else []


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


def run_discovery_for_campaign(
    db: Session,
    campaign_name: str,
    search_query: str,
    daily_count: int = 25,
    search_provider: BusinessSearchProvider | None = None,
    enrichment_provider: ContactEnrichmentProvider | None = None,
) -> tuple[DiscoveryRun, list[Lead]]:
    """Discover up to `daily_count` businesses for one campaign, enrich each
    with a real owner contact, and create a Lead for every one that yields
    real contact data and isn't already a duplicate/suppressed. Always
    returns (DiscoveryRun, newly-created leads) - never raises; the caller
    (app.tasks.celery_tasks.discover_leads_task) queues enrich_lead_task for
    each returned lead."""
    search_provider = search_provider or get_business_search_provider()
    enrichment_provider = enrichment_provider or get_contact_enrichment_provider()

    campaign = _get_or_create_campaign(db, campaign_name)
    # Explicit zeroes: SQLAlchemy's column-level default=0 only applies at
    # INSERT/flush time, not at Python construction, and these fields are
    # incremented with += before this row is ever flushed.
    run = DiscoveryRun(
        campaign_id=campaign.id,
        campaign_name=campaign_name,
        search_query=search_query,
        businesses_found=0,
        leads_created=0,
        duplicates_skipped=0,
        suppressed_skipped=0,
        no_contact_found=0,
    )
    created_leads: list[Lead] = []

    try:
        businesses = search_provider.search(search_query, daily_count)
    except Exception as exc:  # noqa: BLE001 - a provider outage must not crash the beat task
        logger.error("Lead discovery search failed for campaign %r: %s", campaign_name, exc)
        run.status = DiscoveryRunStatus.FAILED
        run.error = str(exc)
        db.add(run)
        db.commit()
        db.refresh(run)
        return run, created_leads

    run.businesses_found = len(businesses)

    for business in businesses:
        try:
            contact = enrichment_provider.find_owner_contact(business)
        except Exception as exc:  # noqa: BLE001 - one bad lookup must not abort the whole run
            logger.warning("Contact enrichment failed for %r: %s", business.name, exc)
            run.no_contact_found += 1
            continue

        if not contact:
            run.no_contact_found += 1
            continue

        email = contact.email.strip().lower()
        if is_suppressed(db, email):
            run.suppressed_skipped += 1
            continue
        if db.query(Lead).filter(Lead.email.ilike(email)).first():
            run.duplicates_skipped += 1
            continue

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
