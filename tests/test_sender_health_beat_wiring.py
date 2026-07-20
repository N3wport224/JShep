"""Lock-in checks for the sender health guardian's thresholds and its
Celery beat wiring (continuous health check + daily warmup rotation)."""
from app.celery_app import celery_app
from app.config import get_settings


def test_bounce_and_spam_thresholds_match_spec():
    settings = get_settings()
    assert settings.bounce_rate_pause_threshold == 0.02
    assert settings.spam_complaint_rate_pause_threshold == 0.001


def test_sender_health_check_task_is_scheduled_every_15_minutes():
    entry = celery_app.conf.beat_schedule["sender-health-check"]
    assert entry["task"] == "app.tasks.celery_tasks.sender_health_check_task"
    assert entry["schedule"] == 900.0


def test_warmup_rotation_task_is_scheduled_daily():
    entry = celery_app.conf.beat_schedule["warmup-rotation"]
    assert entry["task"] == "app.tasks.celery_tasks.warmup_rotation_task"
    assert entry["schedule"] == 86400.0
