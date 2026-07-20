"""Multi-channel (email + LinkedIn) sequencing: channel-aware step
resolution, LinkedIn payload formatting, and the automation stub's
simulate-vs-webhook behavior."""
from unittest.mock import patch

import pytest

from app.models import ChannelType, Lead, SequenceStepConfig
from app.services.linkedin_automation import LinkedInAutomationError, build_payload, execute_touchpoint
from app.services.sequencing import get_sequence_steps, is_linkedin_channel, next_step


def _make_lead(sync_db, linkedin_url="https://linkedin.com/in/janedoe", campaign_id=None):
    lead = Lead(
        company_name="Acme", contact_name="Jane Doe", email="jane-li@acme.example.com",
        linkedin_url=linkedin_url, campaign_id=campaign_id,
    )
    sync_db.add(lead)
    sync_db.commit()
    sync_db.refresh(lead)
    return lead


def test_is_linkedin_channel():
    assert is_linkedin_channel(ChannelType.LINKEDIN_VIEW) is True
    assert is_linkedin_channel(ChannelType.LINKEDIN_CONNECTION) is True
    assert is_linkedin_channel(ChannelType.EMAIL) is False


def test_get_sequence_steps_returns_empty_when_none_configured(sync_db):
    lead = _make_lead(sync_db)
    assert get_sequence_steps(sync_db, lead) == []


def test_get_sequence_steps_uses_global_default_blueprint(sync_db):
    sync_db.add(SequenceStepConfig(campaign_id=None, step_number=1, channel=ChannelType.LINKEDIN_VIEW, delay_days=2))
    sync_db.add(SequenceStepConfig(campaign_id=None, step_number=2, channel=ChannelType.EMAIL, delay_days=3))
    sync_db.commit()

    lead = _make_lead(sync_db)
    steps = get_sequence_steps(sync_db, lead)

    assert [s.step_number for s in steps] == [1, 2]
    assert steps[0].channel == ChannelType.LINKEDIN_VIEW


def test_get_sequence_steps_prefers_campaign_specific_blueprint(sync_db):
    from app.models import Campaign

    campaign = Campaign(name="li-campaign")
    sync_db.add(campaign)
    sync_db.flush()

    sync_db.add(SequenceStepConfig(campaign_id=None, step_number=1, channel=ChannelType.EMAIL, delay_days=1))
    sync_db.add(
        SequenceStepConfig(campaign_id=campaign.id, step_number=1, channel=ChannelType.LINKEDIN_CONNECTION, delay_days=5)
    )
    sync_db.commit()

    lead = _make_lead(sync_db, campaign_id=campaign.id)
    steps = get_sequence_steps(sync_db, lead)

    assert len(steps) == 1
    assert steps[0].channel == ChannelType.LINKEDIN_CONNECTION
    assert steps[0].delay_days == 5


def test_next_step_finds_the_following_step_number():
    steps = [
        SequenceStepConfig(step_number=1, channel=ChannelType.EMAIL, delay_days=3),
        SequenceStepConfig(step_number=2, channel=ChannelType.LINKEDIN_VIEW, delay_days=2),
    ]
    result = next_step(steps, current_step=1)
    assert result.step_number == 2
    assert result.channel == ChannelType.LINKEDIN_VIEW


def test_next_step_returns_none_when_sequence_exhausted():
    steps = [SequenceStepConfig(step_number=1, channel=ChannelType.EMAIL, delay_days=3)]
    assert next_step(steps, current_step=1) is None


def test_build_payload_shape(sync_db):
    lead = _make_lead(sync_db)
    payload = build_payload(lead, ChannelType.LINKEDIN_CONNECTION)
    assert payload == {
        "action": "linkedin_connection",
        "lead_id": lead.id,
        "linkedin_url": lead.linkedin_url,
        "contact_name": lead.contact_name,
        "company_name": lead.company_name,
    }


def test_execute_touchpoint_without_linkedin_url_raises(sync_db):
    from app.models import LinkedInTouchpoint

    lead = _make_lead(sync_db, linkedin_url=None)
    touchpoint = LinkedInTouchpoint(lead_id=lead.id, sequence_step=1, action=ChannelType.LINKEDIN_VIEW)
    sync_db.add(touchpoint)
    sync_db.commit()
    sync_db.refresh(touchpoint)

    with pytest.raises(LinkedInAutomationError):
        execute_touchpoint(touchpoint)


def test_execute_touchpoint_simulates_when_no_webhook_configured(sync_db):
    from app.models import LinkedInTouchpoint

    lead = _make_lead(sync_db)
    touchpoint = LinkedInTouchpoint(lead_id=lead.id, sequence_step=1, action=ChannelType.LINKEDIN_VIEW)
    sync_db.add(touchpoint)
    sync_db.commit()
    sync_db.refresh(touchpoint)

    result = execute_touchpoint(touchpoint)
    assert result == "simulated"


@patch("app.services.linkedin_automation._post_to_automation_webhook")
def test_execute_touchpoint_calls_webhook_when_configured(mock_post, sync_db):
    from app.config import get_settings
    from app.models import LinkedInTouchpoint

    mock_post.return_value = "job-abc-123"
    settings = get_settings()
    settings.linkedin_automation_webhook_url = "https://automation.example.com/hook"
    try:
        lead = _make_lead(sync_db)
        touchpoint = LinkedInTouchpoint(lead_id=lead.id, sequence_step=1, action=ChannelType.LINKEDIN_CONNECTION)
        sync_db.add(touchpoint)
        sync_db.commit()
        sync_db.refresh(touchpoint)

        result = execute_touchpoint(touchpoint)
        assert result == "job-abc-123"
        mock_post.assert_called_once()
    finally:
        settings.linkedin_automation_webhook_url = None
