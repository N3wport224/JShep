"""
Multi-channel outreach sequence resolution. A Campaign can define an
ordered SequenceStepConfig blueprint mixing email and LinkedIn steps; a
lead with no campaign, or whose campaign has no custom blueprint, falls
back to the legacy pure-email FOLLOW_UP_DELAYS_DAYS behavior so existing
deployments keep working unchanged.
"""
from app.models import ChannelType, Lead, SequenceStepConfig


def get_sequence_steps(db, lead: Lead) -> list[SequenceStepConfig]:
    """Ordered step blueprint for this lead: its campaign's steps if any
    are configured, else the global default blueprint (campaign_id IS
    NULL), else an empty list (caller falls back to FOLLOW_UP_DELAYS_DAYS)."""
    if lead.campaign_id:
        campaign_steps = (
            db.query(SequenceStepConfig)
            .filter(SequenceStepConfig.campaign_id == lead.campaign_id)
            .order_by(SequenceStepConfig.step_number)
            .all()
        )
        if campaign_steps:
            return campaign_steps

    return (
        db.query(SequenceStepConfig)
        .filter(SequenceStepConfig.campaign_id.is_(None))
        .order_by(SequenceStepConfig.step_number)
        .all()
    )


def next_step(steps: list[SequenceStepConfig], current_step: int) -> SequenceStepConfig | None:
    target = current_step + 1
    return next((s for s in steps if s.step_number == target), None)


def is_linkedin_channel(channel: ChannelType) -> bool:
    return channel in (ChannelType.LINKEDIN_VIEW, ChannelType.LINKEDIN_CONNECTION)
