"""
A/B campaign variant assignment and performance tracking. A lead is
assigned exactly one variant, once, at first enrichment - it keeps that
variant for its whole lifecycle (including follow-ups) so per-variant
metrics stay statistically clean. Every counter update here mirrors into
the matching Prometheus counter (app.core.metrics) for dashboards, with the
CampaignVariant row remaining the source of truth for the JSON stats API.
"""
import hashlib
import logging

from sqlalchemy.orm import Session

from app.core.metrics import variant_positive_total, variant_reply_total, variant_sent_total
from app.models import Campaign, CampaignVariant, Lead

logger = logging.getLogger(__name__)


def assign_variant(db: Session, lead: Lead) -> CampaignVariant | None:
    """Deterministically assign one of the campaign's variants to a lead,
    weighted by CampaignVariant.weight. Deterministic (hash of lead.id)
    rather than random so re-running enrichment for the same lead never
    reassigns it to a different variant."""
    if lead.variant_id:
        return lead.variant
    if not lead.campaign_id:
        return None

    campaign = db.get(Campaign, lead.campaign_id)
    if not campaign or not campaign.variants:
        return None

    total_weight = sum(max(v.weight, 0) for v in campaign.variants) or len(campaign.variants)
    bucket = int(hashlib.sha256(lead.id.encode()).hexdigest(), 16) % total_weight

    cumulative = 0
    chosen = campaign.variants[0]
    for variant in campaign.variants:
        cumulative += max(variant.weight, 0) or 1
        if bucket < cumulative:
            chosen = variant
            break

    # Assign via the relationship, not the raw FK column - lead.variant is
    # eagerly loaded (lazy="selectin"), so setting only lead.variant_id
    # leaves the already-loaded .variant attribute stale (still None) for
    # the rest of this in-memory object's lifetime.
    lead.variant = chosen
    db.flush()
    logger.info("Assigned lead %s to campaign %s variant %s", lead.id, campaign.name, chosen.label)
    return chosen


def record_sent(db: Session, lead: Lead) -> None:
    if not lead.variant_id:
        return
    variant: CampaignVariant = lead.variant
    variant.sent_count += 1
    variant_sent_total.labels(campaign=variant.campaign.name, variant=variant.label).inc()
    db.commit()


def record_reply(db: Session, lead: Lead) -> None:
    if not lead.variant_id:
        return
    variant: CampaignVariant = lead.variant
    variant.reply_count += 1
    variant_reply_total.labels(campaign=variant.campaign.name, variant=variant.label).inc()
    db.commit()


def record_positive(db: Session, lead: Lead) -> None:
    if not lead.variant_id:
        return
    variant: CampaignVariant = lead.variant
    variant.positive_count += 1
    variant_positive_total.labels(campaign=variant.campaign.name, variant=variant.label).inc()
    db.commit()
