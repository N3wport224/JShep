"""
LinkedIn touchpoint automation stub. This service NEVER touches LinkedIn
itself - it formats a safe, provider-agnostic payload describing the
requested action (profile view or connection request) and hands it to
whatever headless-browser automation layer is configured
(LINKEDIN_AUTOMATION_WEBHOOK_URL - e.g. a PhantomBuster Phantom's launch
webhook, or a local Playwright worker's job queue endpoint).

With no webhook configured, execution is safely simulated: the payload is
formatted and logged exactly as it would be sent, and the touchpoint is
marked executed, so multi-channel sequences work end to end in dev/test
without a real automation backend wired up.
"""
import logging

import httpx

from app.config import get_settings
from app.core.metrics import linkedin_touchpoint_total
from app.core.resilience import RateLimitedError, get_circuit_breaker, resilient_retry
from app.models import ChannelType, Lead, LinkedInTouchpoint

logger = logging.getLogger(__name__)


class LinkedInAutomationError(Exception):
    pass


def build_payload(lead: Lead, action: ChannelType) -> dict:
    """The exact, provider-agnostic job description handed to the
    automation layer. Kept as a plain dict (not a provider-specific shape)
    so swapping PhantomBuster for a local Playwright worker - or anything
    else - only requires changing execute_touchpoint below, not every
    caller."""
    return {
        "action": action.value,
        "lead_id": lead.id,
        "linkedin_url": lead.linkedin_url,
        "contact_name": lead.contact_name,
        "company_name": lead.company_name,
    }


@resilient_retry(retryable_exceptions=(RateLimitedError, ConnectionError, TimeoutError))
def _post_to_automation_webhook(payload: dict) -> str:
    settings = get_settings()
    breaker = get_circuit_breaker("linkedin_automation")
    breaker.before_call()
    try:
        headers = {}
        if settings.linkedin_automation_api_key:
            headers["Authorization"] = f"Bearer {settings.linkedin_automation_api_key}"
        resp = httpx.post(settings.linkedin_automation_webhook_url, json=payload, headers=headers, timeout=20)
        if resp.status_code == 429:
            raise RateLimitedError(f"LinkedIn automation webhook rate limited: {resp.text}")
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        breaker.on_failure()
        raise LinkedInAutomationError(str(exc)) from exc
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        breaker.on_failure()
        raise ConnectionError(str(exc)) from exc
    else:
        breaker.on_success()
        try:
            return resp.json().get("job_id", "")
        except ValueError:
            return ""


def execute_touchpoint(touchpoint: LinkedInTouchpoint) -> str:
    """Execute (or simulate) one LinkedIn touchpoint. Returns an external
    reference ID (the automation layer's job ID, or "simulated" when no
    webhook is configured). Raises LinkedInAutomationError/ConnectionError
    on failure - callers should mark the touchpoint FAILED and let the
    Celery retry policy handle transient failures."""
    settings = get_settings()
    lead = touchpoint.lead

    if not lead.linkedin_url:
        raise LinkedInAutomationError(f"Lead {lead.id} has no linkedin_url - cannot execute {touchpoint.action.value}")

    payload = build_payload(lead, touchpoint.action)

    if not settings.linkedin_automation_webhook_url:
        logger.info(
            "LinkedIn automation not configured - simulating %s for lead %s: %s",
            touchpoint.action.value,
            lead.id,
            payload,
        )
        linkedin_touchpoint_total.labels(action=touchpoint.action.value, status="simulated").inc()
        return "simulated"

    try:
        job_id = _post_to_automation_webhook(payload)
    except Exception:
        linkedin_touchpoint_total.labels(action=touchpoint.action.value, status="failed").inc()
        raise
    else:
        linkedin_touchpoint_total.labels(action=touchpoint.action.value, status="executed").inc()
        return job_id or "executed"
