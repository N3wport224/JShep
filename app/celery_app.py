"""
Celery application: the async task queue backing all heavy-lifting work
(batch lead enrichment, email dispatch, IMAP polling, follow-up sequencing,
reply triage). Replaces the old in-process APScheduler - Celery beat drives
the periodic jobs, and the FastAPI web layer only ever enqueues tasks, never
runs LLM/SMTP/IMAP calls inline on the request path.
"""
from celery import Celery
from celery.schedules import crontab

from app.config import get_settings
from app.core.logging import configure_logging

settings = get_settings()
configure_logging()

celery_app = Celery(
    "sdr",
    broker=settings.resolved_celery_broker_url,
    backend=settings.resolved_celery_result_backend,
    include=["app.tasks.celery_tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_default_retry_delay=10,
    broker_connection_retry_on_startup=True,
)

celery_app.conf.beat_schedule = {
    "poll-inbox": {
        "task": "app.tasks.celery_tasks.poll_imap_task",
        "schedule": settings.imap_poll_interval_seconds,
    },
    "follow-up-sequence": {
        "task": "app.tasks.celery_tasks.follow_up_sequence_task",
        "schedule": 1800.0,  # every 30 minutes
    },
    "sender-health-check": {
        "task": "app.tasks.celery_tasks.sender_health_check_task",
        "schedule": 900.0,  # every 15 minutes
    },
    "warmup-rotation": {
        "task": "app.tasks.celery_tasks.warmup_rotation_task",
        "schedule": 86400.0,  # once a day
    },
    "lead-discovery": {
        "task": "app.tasks.celery_tasks.discover_leads_task",
        # Once daily at 08:00 UTC - a fixed time (not a rolling interval)
        # since "pull 25 target businesses daily" reads as a daily quota,
        # not a from-whenever-the-worker-started cadence. No-ops internally
        # if LEAD_DISCOVERY_ENABLED is false.
        "schedule": crontab(hour=8, minute=0),
    },
}
