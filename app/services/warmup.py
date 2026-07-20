"""
Inbox warmup pool rotation. A brand-new sending identity that blasts at full
volume from day one gets flagged as spam; warmup instead ramps daily_limit
through a sequence of stages over time. This is a simulated/local warmup
scheduler - integrating a real warmup pool provider (e.g. Mailwarm-style
seed-account networks) would slot in here by having advance_warmup also
trigger that provider's API, but the staged daily_limit ramp and daily
counter reset are what actually gate outbound volume regardless.

Run daily by Celery beat (see app.tasks.celery_tasks.warmup_rotation_task).
"""
import logging

from sqlalchemy.orm import Session

from app.core.metrics import sender_daily_limit, sender_warmup_stage
from app.models import SenderAccount

logger = logging.getLogger(__name__)


def warmup_targets(settings) -> list[int]:
    return [int(x.strip()) for x in settings.warmup_daily_targets.split(",") if x.strip()]


def advance_warmup(db: Session, settings) -> dict:
    """For every non-paused account not yet fully warmed up, move to the
    next stage's daily_limit. Also resets the rolling daily
    sent/bounce/open/spam_complaint counters for every account (paused ones
    included, so a healthy stretch can bring bounce/spam rate back down and
    make an account eligible for manual re-enable) - the counters on
    SenderAccount represent "since counters_reset_at", not all-time totals.
    """
    from datetime import datetime

    targets = warmup_targets(settings)
    advanced = []

    accounts = db.query(SenderAccount).all()
    for account in accounts:
        if not settings.warmup_enabled:
            sender_warmup_stage.labels(sender_account=account.name).set(account.warmup_stage)
            sender_daily_limit.labels(sender_account=account.name).set(account.daily_limit)
            continue

        if not account.is_paused and targets and not account.warmup_complete:
            next_stage = account.warmup_stage + 1
            if next_stage < len(targets):
                account.warmup_stage = next_stage
                account.daily_limit = targets[next_stage]
                advanced.append({"account": account.name, "stage": next_stage, "daily_limit": account.daily_limit})
            else:
                account.warmup_complete = True
                logger.info("Sender account %s completed inbox warmup", account.name)

        # Roll the daily window: today's counters become tomorrow's history.
        account.sent_count = 0
        account.bounce_count = 0
        account.open_count = 0
        account.spam_complaint_count = 0
        account.counters_reset_at = datetime.utcnow()

        sender_warmup_stage.labels(sender_account=account.name).set(account.warmup_stage)
        sender_daily_limit.labels(sender_account=account.name).set(account.daily_limit)

    db.commit()
    if advanced:
        logger.info("Advanced warmup stage for %d sender account(s)", len(advanced))
    return {"accounts_processed": len(accounts), "advanced": advanced}
