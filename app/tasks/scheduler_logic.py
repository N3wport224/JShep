"""Small pure helpers shared between the Celery send task and (indirectly)
the follow-up sequence task. Kept separate from celery_tasks.py to avoid a
circular import (send_email_task needs this; nothing else does)."""
from datetime import datetime, timedelta

from app.models import Lead
from app.services.sequencing import get_sequence_steps, next_step


def _parse_follow_up_delays(settings) -> list[int]:
    return [int(x.strip()) for x in settings.follow_up_delays_days.split(",") if x.strip()]


def arm_first_follow_up(settings, lead: Lead, db=None) -> None:
    """Call right after the initial cold email is sent to schedule the first
    follow-up step. The Celery beat 'follow-up-sequence' task picks up any
    lead whose next_follow_up_at has elapsed.

    If the lead's campaign has a custom multi-channel SequenceStepConfig
    blueprint (db must be passed to check), step 1's delay_days is used;
    otherwise falls back to the legacy FOLLOW_UP_DELAYS_DAYS email-only
    sequence."""
    if db is not None:
        steps = get_sequence_steps(db, lead)
        step_one = next_step(steps, 0)
        if step_one:
            lead.next_follow_up_at = datetime.utcnow() + timedelta(days=step_one.delay_days)
            return

    delays = _parse_follow_up_delays(settings)
    if delays:
        lead.next_follow_up_at = datetime.utcnow() + timedelta(days=delays[0])
