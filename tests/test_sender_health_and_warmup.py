"""Sender health guardian: spam-complaint auto-pause, bounce+spam threshold
interplay, and warmup stage advancement / daily counter reset."""
from app.config import get_settings
from app.models import SenderAccount, SenderProvider
from app.services.sender_rotation import record_bounce, record_spam_complaint
from app.services.warmup import advance_warmup, warmup_targets


def _make_account(sync_db, **overrides):
    defaults = dict(
        name="warmup-acct",
        provider=SenderProvider.SMTP,
        from_email="sender@example.com",
        from_name="Sales",
        smtp_host="smtp.example.com",
        smtp_port=587,
        sent_count=0,
        bounce_count=0,
        spam_complaint_count=0,
    )
    defaults.update(overrides)
    account = SenderAccount(**defaults)
    sync_db.add(account)
    sync_db.commit()
    sync_db.refresh(account)
    return account


def test_bounce_threshold_is_2_percent_by_default():
    assert get_settings().bounce_rate_pause_threshold == 0.02


def test_spam_complaint_threshold_is_point_one_percent_by_default():
    assert get_settings().spam_complaint_rate_pause_threshold == 0.001


def test_account_auto_pauses_at_2_percent_bounce_rate(sync_db):
    account = _make_account(sync_db, sent_count=100, bounce_count=1)
    record_bounce(sync_db, account)  # 2/100 = 2% >= 2% threshold
    assert account.is_paused is True
    assert "bounce rate" in account.pause_reason.lower()


def test_account_not_paused_below_2_percent_bounce_rate(sync_db):
    account = _make_account(sync_db, sent_count=1000, bounce_count=1)
    record_bounce(sync_db, account)  # 2/1000 = 0.2%, well under 2%
    assert account.is_paused is False


def test_account_auto_pauses_at_point_one_percent_spam_complaint_rate(sync_db):
    account = _make_account(sync_db, sent_count=1000, bounce_count=0, spam_complaint_count=0)
    record_spam_complaint(sync_db, account)  # 1/1000 = 0.1% >= 0.1% threshold
    assert account.is_paused is True
    assert "spam complaint" in account.pause_reason.lower()


def test_account_not_paused_below_spam_complaint_threshold(sync_db):
    account = _make_account(sync_db, sent_count=1000, spam_complaint_count=0)
    # 0/1000 -> after one complaint: 1/1000 = exactly threshold, use a
    # larger sample so it's clearly under.
    account.sent_count = 10000
    sync_db.commit()
    record_spam_complaint(sync_db, account)  # 1/10000 = 0.01%, under 0.1%
    assert account.is_paused is False


def test_spam_complaint_pause_respects_min_sample_size(sync_db):
    # Even a 100% spam-complaint rate shouldn't pause a brand-new account
    # with too few sends to be statistically meaningful.
    account = _make_account(sync_db, sent_count=1, spam_complaint_count=0)
    record_spam_complaint(sync_db, account)
    assert account.is_paused is False


def test_warmup_targets_parsed_from_settings():
    settings = get_settings()
    targets = warmup_targets(settings)
    assert targets == [10, 20, 40, 80, 150, 200]


def test_advance_warmup_moves_account_to_next_stage(sync_db):
    account = _make_account(sync_db, warmup_stage=0, daily_limit=10, sent_count=8, bounce_count=0)
    settings = get_settings()

    result = advance_warmup(sync_db, settings)

    sync_db.refresh(account)
    assert account.warmup_stage == 1
    assert account.daily_limit == 20  # second target in the default ramp
    assert len(result["advanced"]) == 1


def test_advance_warmup_resets_daily_counters(sync_db):
    account = _make_account(sync_db, sent_count=15, bounce_count=2, open_count=5, spam_complaint_count=1)
    advance_warmup(sync_db, get_settings())
    sync_db.refresh(account)
    assert account.sent_count == 0
    assert account.bounce_count == 0
    assert account.open_count == 0
    assert account.spam_complaint_count == 0


def test_advance_warmup_skips_paused_accounts_stage_but_still_resets_counters(sync_db):
    account = _make_account(sync_db, warmup_stage=0, daily_limit=10, is_paused=True, pause_reason="bad", sent_count=5)
    advance_warmup(sync_db, get_settings())
    sync_db.refresh(account)
    assert account.warmup_stage == 0  # not advanced while paused
    assert account.is_paused is True  # stays paused - requires manual re-enable
    assert account.sent_count == 0  # counters still reset


def test_advance_warmup_marks_complete_after_final_stage(sync_db):
    account = _make_account(sync_db, warmup_stage=5, daily_limit=200)  # already at the last target
    advance_warmup(sync_db, get_settings())
    sync_db.refresh(account)
    assert account.warmup_complete is True
    assert account.warmup_stage == 5  # doesn't overflow past the last stage


def test_advance_warmup_noop_when_warmup_disabled(sync_db):
    from copy import copy

    account = _make_account(sync_db, warmup_stage=0, daily_limit=10)
    settings = copy(get_settings())
    settings.warmup_enabled = False

    advance_warmup(sync_db, settings)

    sync_db.refresh(account)
    assert account.warmup_stage == 0
    assert account.daily_limit == 10
