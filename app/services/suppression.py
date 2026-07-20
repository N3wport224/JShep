"""
Global suppression/do-not-contact list (CAN-SPAM/GDPR compliance).
`is_suppressed` is the mandatory gate every outbound send path checks
before dispatching anything - cold emails, follow-ups, and approved
human replies alike - so a suppressed email or domain can never be
targeted again, even by accident (a stale queued task, a re-uploaded
CSV, a new campaign).
"""
import logging

from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.metrics import suppression_total
from app.models import Lead, SuppressionEntry, SuppressionSource

logger = logging.getLogger(__name__)


def _domain_of(email: str) -> str:
    return email.strip().lower().rsplit("@", 1)[-1]


def is_suppressed(db: Session, email: str) -> bool:
    email = email.strip().lower()
    domain = _domain_of(email)
    return (
        db.query(SuppressionEntry)
        .filter((SuppressionEntry.email == email) | (SuppressionEntry.domain == domain))
        .first()
        is not None
    )


def add_to_suppression(
    db: Session, email: str, source: SuppressionSource, reason: str | None = None
) -> SuppressionEntry:
    email = email.strip().lower()
    domain = _domain_of(email)

    existing = db.query(SuppressionEntry).filter(SuppressionEntry.email == email).first()
    if existing:
        return existing

    entry = SuppressionEntry(email=email, domain=domain, reason=reason, source=source)
    db.add(entry)
    db.commit()
    db.refresh(entry)
    suppression_total.labels(source=source.value).inc()
    logger.info("Added %s to global suppression list (source=%s, reason=%s)", email, source.value, reason)
    return entry


def unsubscribe_footer(lead: Lead) -> str:
    """CAN-SPAM requires a working opt-out mechanism in commercial email.
    Appended to cold emails and follow-ups (not to a human-approved
    conversational reply, which isn't bulk marketing)."""
    settings = get_settings()
    url = f"{settings.api_base_url}/unsubscribe/{lead.id}?token={lead.unsubscribe_token}"
    return f"\n\n---\nIf you'd rather not hear from us again, unsubscribe here: {url}"
