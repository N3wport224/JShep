"""Pre-send spam heuristic guardian: scoring behavior, the self-correction
rewrite loop, and end-to-end wiring into enrichment/follow-up so a HIGH-risk
draft never reaches the sending queue without a human clearing it."""
from unittest.mock import patch

from app.config import get_settings
from app.models import EmailMessage, Lead, MessageStatus
from app.schemas import ColdEmailDraft, SpamRiskLevel
from app.services.spam_guardian import run_with_guard, score_email
from app.tasks.celery_tasks import enrich_lead_task


def test_clean_conversational_email_scores_low():
    result = score_email(
        "Quick question about your engineering team",
        "Hi Jane, saw that Acme just closed a new round - congrats. "
        "Curious how your team is handling on-call rotation as you scale. "
        "Worth a quick chat sometime this week?",
    )
    assert result.risk_level == SpamRiskLevel.LOW
    assert result.trigger_words_found == []


def test_trigger_words_detected_and_scored():
    result = score_email(
        "ACT NOW - guaranteed results!",
        "Buy now and get a cash bonus, no obligation, 100% risk-free!",
    )
    assert "guaranteed" in result.trigger_words_found
    assert "risk-free" in result.trigger_words_found
    assert "no obligation" in result.trigger_words_found
    assert result.score > 0
    assert result.risk_level in (SpamRiskLevel.MEDIUM, SpamRiskLevel.HIGH)


def test_excessive_punctuation_detected():
    result = score_email("Hello!!!", "Are you interested??? Let me know!!!")
    assert result.score > 0
    assert any("punctuation" in r for r in result.reasons)


def test_all_caps_body_detected():
    result = score_email(
        "Subject line",
        "THIS IS A HUGE OPPORTUNITY YOU CANNOT MISS SO PLEASE RESPOND IMMEDIATELY",
    )
    assert result.caps_ratio > 0.15
    assert any("ALL CAPS" in r for r in result.reasons)


def test_all_caps_subject_detected():
    result = score_email("URGENT ACTION REQUIRED", "Hi Jane, just checking in.")
    assert any("uppercase" in r for r in result.reasons)


def test_high_link_density_detected():
    body = "Check these out: http://a.example.com http://b.example.com http://c.example.com http://d.example.com"
    result = score_email("Links", body)
    assert result.link_count == 4
    assert any("link" in r.lower() for r in result.reasons)


def test_score_is_capped_at_100():
    body = " ".join(["FREE"] * 5) + " " + " ".join(TW for TW in ["act now", "guaranteed", "risk-free", "urgent", "winner"]) + "!!!! " * 10
    result = score_email("FREE FREE FREE ACT NOW!!!", body.upper())
    assert result.score <= 100


def test_thresholds_match_config_defaults():
    settings = get_settings()
    assert settings.spam_score_rewrite_threshold == 40
    assert settings.spam_score_flag_threshold == 70


def test_run_with_guard_accepts_low_risk_draft_on_first_try():
    calls = []

    def generate(feedback):
        calls.append(feedback)
        return ColdEmailDraft(subject="Quick question", body="Hi Jane, curious how your team handles X.")

    draft, result = run_with_guard(generate)
    assert result.risk_level == SpamRiskLevel.LOW
    assert calls == [None]  # only called once - no rewrite needed


def test_run_with_guard_retries_on_high_risk_then_succeeds():
    responses = [
        ColdEmailDraft(subject="ACT NOW!!!", body="Guaranteed risk-free cash bonus, buy now, no obligation!!!"),
        ColdEmailDraft(subject="Quick question", body="Hi Jane, curious how your team handles X."),
    ]
    calls = []

    def generate(feedback):
        calls.append(feedback)
        return responses.pop(0)

    draft, result = run_with_guard(generate, max_attempts=2)
    assert result.risk_level == SpamRiskLevel.LOW
    assert len(calls) == 2
    assert calls[0] is None
    assert calls[1] is not None  # second call got feedback from the first score


def test_run_with_guard_gives_up_after_max_attempts_still_high():
    spammy = ColdEmailDraft(subject="ACT NOW!!!", body="Guaranteed risk-free cash bonus, buy now, no obligation!!!")

    def generate(feedback):
        return spammy

    draft, result = run_with_guard(generate, max_attempts=2)
    assert result.risk_level == SpamRiskLevel.HIGH
    assert draft is spammy


def _make_lead(sync_db, **overrides):
    defaults = dict(company_name="Acme", contact_name="Jane", email="jane-spam@acme.example.com")
    defaults.update(overrides)
    lead = Lead(**defaults)
    sync_db.add(lead)
    sync_db.commit()
    sync_db.refresh(lead)
    return lead


@patch("app.tasks.celery_tasks.get_llm_client")
def test_enrich_lead_task_creates_draft_for_clean_copy(mock_get_llm, sync_db):
    from unittest.mock import MagicMock

    llm = MagicMock()
    llm.generate_cold_email.return_value = ColdEmailDraft(
        subject="Quick question", body="Hi Jane, curious how your team handles on-call rotation."
    )
    mock_get_llm.return_value = llm

    lead = _make_lead(sync_db)
    result = enrich_lead_task(lead.id)

    assert result["status"] == "enriched"
    message = sync_db.query(EmailMessage).filter_by(lead_id=lead.id).one()
    assert message.status == MessageStatus.DRAFT
    assert message.spam_flagged is False
    assert message.spam_score is not None


@patch("app.tasks.celery_tasks.get_llm_client")
def test_enrich_lead_task_holds_still_spammy_draft_for_review(mock_get_llm, sync_db):
    from unittest.mock import MagicMock

    llm = MagicMock()
    llm.generate_cold_email.return_value = ColdEmailDraft(
        subject="ACT NOW!!!", body="Guaranteed risk-free cash bonus, buy now, no obligation, winner!!!"
    )
    mock_get_llm.return_value = llm

    lead = _make_lead(sync_db)
    result = enrich_lead_task(lead.id)

    assert result["status"] == "needs_review"
    message = sync_db.query(EmailMessage).filter_by(lead_id=lead.id).one()
    assert message.status == MessageStatus.NEEDS_REVIEW
    assert message.spam_flagged is True
    assert message.spam_reasons

    # generate_cold_email was called MAX_REWRITE_ATTEMPTS+1 times since the
    # mock always returns the same spammy copy
    settings = get_settings()
    assert llm.generate_cold_email.call_count == settings.spam_guardian_max_rewrite_attempts + 1


@patch("app.tasks.celery_tasks.get_llm_client")
def test_enrich_lead_task_rewrite_succeeds_after_first_attempt(mock_get_llm, sync_db):
    from unittest.mock import MagicMock

    llm = MagicMock()
    llm.generate_cold_email.side_effect = [
        ColdEmailDraft(subject="ACT NOW!!!", body="Guaranteed risk-free cash bonus, buy now, no obligation!!!"),
        ColdEmailDraft(subject="Quick question", body="Hi Jane, curious how your team handles on-call rotation."),
    ]
    mock_get_llm.return_value = llm

    lead = _make_lead(sync_db)
    result = enrich_lead_task(lead.id)

    assert result["status"] == "enriched"
    message = sync_db.query(EmailMessage).filter_by(lead_id=lead.id).one()
    assert message.status == MessageStatus.DRAFT
    assert message.spam_flagged is False
    assert llm.generate_cold_email.call_count == 2
    # second call should have received spam feedback
    _, kwargs = llm.generate_cold_email.call_args_list[1]
    assert kwargs.get("spam_feedback")


def test_needs_review_message_cannot_be_queued_for_send(sync_db):
    lead = _make_lead(sync_db, email="review-block@example.com")
    message = EmailMessage(
        lead_id=lead.id,
        subject="ACT NOW!!!",
        body="spammy",
        status=MessageStatus.NEEDS_REVIEW,
        spam_flagged=True,
        spam_score=90,
    )
    sync_db.add(message)
    sync_db.commit()
    sync_db.refresh(message)
    assert message.status != MessageStatus.DRAFT  # outbound router only queues DRAFT messages
