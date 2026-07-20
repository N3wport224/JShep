"""Sender account bounce-rate health tracking and auto-pause."""
from app.models import SenderAccount, SenderProvider
from app.services.sender_rotation import record_bounce


def _make_account(sync_db, **overrides) -> SenderAccount:
    defaults = dict(
        name="acct-under-test",
        provider=SenderProvider.SMTP,
        from_email="sender@example.com",
        from_name="Sales",
        smtp_host="smtp.example.com",
        smtp_port=587,
        sent_count=0,
        bounce_count=0,
    )
    defaults.update(overrides)
    account = SenderAccount(**defaults)
    sync_db.add(account)
    sync_db.commit()
    sync_db.refresh(account)
    return account


def test_bounce_rate_zero_sends_is_zero_not_division_error(sync_db):
    account = _make_account(sync_db, sent_count=0, bounce_count=0)
    assert account.bounce_rate == 0.0


def test_account_not_paused_below_min_sample_even_at_high_bounce_rate(sync_db):
    # 2/3 bounced is way over threshold, but min sample size hasn't been hit yet
    account = _make_account(sync_db, sent_count=3, bounce_count=2)
    record_bounce(sync_db, account)
    assert account.is_paused is False


def test_account_auto_pauses_once_threshold_and_min_sample_both_exceeded(sync_db):
    # default threshold 0.05, min sample 20 (from app.config.Settings defaults)
    account = _make_account(sync_db, sent_count=20, bounce_count=0)
    for _ in range(2):  # push bounce_count from 0 -> 2, rate 2/20 = 0.10 >= 0.05
        record_bounce(sync_db, account)
    assert account.is_paused is True
    assert "bounce rate" in account.pause_reason.lower()


def test_account_stays_active_below_threshold(sync_db):
    account = _make_account(sync_db, sent_count=100, bounce_count=0)
    record_bounce(sync_db, account)  # 1/100 = 0.01, below 0.05 threshold
    assert account.is_paused is False


def test_already_paused_account_is_not_re_paused_with_new_reason(sync_db):
    account = _make_account(sync_db, sent_count=20, bounce_count=5, is_paused=True, pause_reason="manual pause")
    record_bounce(sync_db, account)
    assert account.pause_reason == "manual pause"  # untouched - already paused
