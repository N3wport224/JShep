"""Small pure helpers shared between the Celery send task and (indirectly)
the follow-up sequence task. Kept separate from celery_tasks.py to avoid a
circular import (send_email_task needs this; nothing else does)."""
from datetime import datetime, timedelta

from app.models import Lead


def _parse_follow_up_delays(settings) -> list[int]:
    return [int(x.strip()) for x in settings.follow_up_delays_days.split(",") if x.strip()]


def arm_first_follow_up(settings, lead: Lead) -> None:
    """Call right after the initial cold email is sent to schedule the first
    follow-up step. The Celery beat 'follow-up-sequence' task picks up any
    lead whose next_follow_up_at has elapsed."""
    delays = _parse_follow_up_delays(settings)
    if delays:
        lead.next_follow_up_at = datetime.utcnow() + timedelta(days=delays[0])
