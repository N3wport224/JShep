"""
CRM sync. When a lead's reply is classified Positive/Interested, its
contact record is pushed to the configured CRM (currently HubSpot) so sales
ops sees it without waiting on the human-in-the-loop approval step. This is
purely additive record-keeping - it never overlaps with the approval gate
that guards outbound replies.

To add another CRM, implement CRMProvider and register it in
get_crm_provider() below.
"""
import abc
import logging

import httpx

from app.config import get_settings
from app.core.metrics import crm_sync_total
from app.core.resilience import RateLimitedError, get_circuit_breaker, resilient_retry
from app.models import Lead

logger = logging.getLogger(__name__)


class CRMSyncError(Exception):
    pass


class CRMProvider(abc.ABC):
    name: str

    @abc.abstractmethod
    def push_contact(self, lead: Lead) -> str:
        """Create/update a CRM contact for this lead and return the CRM's
        contact ID."""


class HubSpotCRM(CRMProvider):
    name = "hubspot"
    api_base_url = "https://api.hubapi.com"

    @resilient_retry(retryable_exceptions=(RateLimitedError, ConnectionError, TimeoutError))
    def push_contact(self, lead: Lead) -> str:
        settings = get_settings()
        if not settings.hubspot_access_token:
            raise CRMSyncError("HUBSPOT_ACCESS_TOKEN is not configured")

        breaker = get_circuit_breaker("crm:hubspot")
        breaker.before_call()

        payload = {
            "properties": {
                "email": lead.email,
                "firstname": lead.contact_name.split(" ")[0],
                "lastname": " ".join(lead.contact_name.split(" ")[1:]) or lead.contact_name,
                "company": lead.company_name,
                "website": lead.website or "",
                "lifecyclestage": "salesqualifiedlead",
            }
        }
        headers = {"Authorization": f"Bearer {settings.hubspot_access_token}", "Content-Type": "application/json"}

        try:
            resp = httpx.post(
                f"{self.api_base_url}/crm/v3/objects/contacts", json=payload, headers=headers, timeout=20
            )
            if resp.status_code == 429:
                raise RateLimitedError(f"HubSpot rate limited: {resp.text}")
            if resp.status_code == 409:
                # Contact already exists (matched on email) - HubSpot returns
                # the existing object id in the error body.
                existing_id = resp.json().get("message", "")
                breaker.on_success()
                contact_id = self._extract_conflicting_id(existing_id) or lead.crm_contact_id or "unknown"
                return contact_id
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            breaker.on_failure()
            raise CRMSyncError(str(exc)) from exc
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            breaker.on_failure()
            raise ConnectionError(str(exc)) from exc
        else:
            breaker.on_success()
            return resp.json()["id"]

    @staticmethod
    def _extract_conflicting_id(message: str) -> str | None:
        # HubSpot's 409 message looks like: "Contact already exists. Existing ID: 12345"
        import re

        match = re.search(r"Existing ID:\s*(\d+)", message)
        return match.group(1) if match else None


def get_crm_provider() -> CRMProvider | None:
    settings = get_settings()
    if settings.crm_provider == "hubspot":
        return HubSpotCRM()
    return None


def sync_lead_to_crm(db, lead: Lead) -> None:
    """Push a lead to the configured CRM and record the resulting contact
    ID. No-op (logs and returns) if no CRM is configured."""
    provider = get_crm_provider()
    if provider is None:
        logger.debug("No CRM provider configured; skipping sync for lead %s", lead.id)
        return

    from datetime import datetime

    try:
        contact_id = provider.push_contact(lead)
    except Exception as exc:  # noqa: BLE001 - never let a CRM outage break triage
        logger.error("CRM sync failed for lead %s via %s: %s", lead.id, provider.name, exc)
        crm_sync_total.labels(provider=provider.name, status="failed").inc()
        return

    lead.crm_contact_id = contact_id
    lead.crm_synced_at = datetime.utcnow()
    db.commit()
    crm_sync_total.labels(provider=provider.name, status="success").inc()
    logger.info("Synced lead %s to %s as contact %s", lead.id, provider.name, contact_id)
