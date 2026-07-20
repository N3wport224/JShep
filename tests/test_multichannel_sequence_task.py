"""follow_up_sequence_task's channel-aware branching: a configured
SequenceStepConfig blueprint routes LinkedIn steps to a queued touchpoint
instead of an email, while leads with no blueprint keep the legacy
pure-email behavior unchanged."""
from datetime import datetime, timedelta
from unittest.mock import patch

from app.models import (
    ChannelType,
    EmailMessage,
    Lead,
    LeadStatus,
    LinkedInTouchpoint,
    MessageDirection,
    MessageStatus,
    SequenceStepConfig,
    TouchpointStatus,
)
from app.tasks.celery_tasks import follow_up_sequence_task


def _make_sent_lead(sync_db, campaign_id=None, follow_up_step=0):
    lead = Lead(
        company_name="Acme",
        contact_name="Jane",
        email="jane-seq@acme.example.com",
        linkedin_url="https://linkedin.com/in/jane",
        status=LeadStatus.SENT,
        campaign_id=campaign_id,
        follow_up_step=follow_up_step,
        next_follow_up_at=datetime.utcnow() - timedelta(minutes=1),  # due now
    )
    sync_db.add(lead)
    sync_db.flush()

    message = EmailMessage(
        lead_id=lead.id,
        direction=MessageDirection.OUTBOUND,
        sequence_step=0,
        subject="Intro",
        body="Hi Jane",
        status=MessageStatus.SENT,
        sent_at=datetime.utcnow() - timedelta(days=1),
    )
    sync_db.add(message)
    sync_db.commit()
    sync_db.refresh(lead)
    return lead


@patch("app.tasks.celery_tasks.execute_linkedin_touchpoint_task.delay")
def test_linkedin_step_queues_touchpoint_not_email(mock_delay, sync_db):
    lead = _make_sent_lead(sync_db, follow_up_step=0)
    sync_db.add(
        SequenceStepConfig(campaign_id=None, step_number=1, channel=ChannelType.LINKEDIN_VIEW, delay_days=2)
    )
    sync_db.commit()

    result = follow_up_sequence_task()

    assert result["follow_ups_queued"] == 1
    mock_delay.assert_called_once()

    touchpoints = sync_db.query(LinkedInTouchpoint).filter_by(lead_id=lead.id).all()
    assert len(touchpoints) == 1
    assert touchpoints[0].action == ChannelType.LINKEDIN_VIEW
    assert touchpoints[0].status == TouchpointStatus.QUEUED

    sync_db.refresh(lead)
    assert lead.follow_up_step == 1


@patch("app.tasks.celery_tasks.send_email_task.delay")
@patch("app.tasks.celery_tasks.get_llm_client")
def test_email_step_in_channel_aware_sequence_still_sends_email(mock_get_llm, mock_send_delay, sync_db):
    from unittest.mock import MagicMock

    from app.schemas import ColdEmailDraft

    llm = MagicMock()
    llm.generate_follow_up_email.return_value = ColdEmailDraft(subject="Following up", body="Just checking in")
    mock_get_llm.return_value = llm

    lead = _make_sent_lead(sync_db, follow_up_step=0)
    sync_db.add(SequenceStepConfig(campaign_id=None, step_number=1, channel=ChannelType.EMAIL, delay_days=3))
    sync_db.commit()

    result = follow_up_sequence_task()

    assert result["follow_ups_queued"] == 1
    mock_send_delay.assert_called_once()
    sync_db.refresh(lead)
    assert lead.follow_up_step == 1


@patch("app.tasks.celery_tasks.execute_linkedin_touchpoint_task.delay")
def test_sequence_exhausted_clears_next_follow_up(mock_delay, sync_db):
    lead = _make_sent_lead(sync_db, follow_up_step=1)  # already past the only configured step
    sync_db.add(
        SequenceStepConfig(campaign_id=None, step_number=1, channel=ChannelType.LINKEDIN_VIEW, delay_days=2)
    )
    sync_db.commit()

    follow_up_sequence_task()

    mock_delay.assert_not_called()
    sync_db.refresh(lead)
    assert lead.next_follow_up_at is None


@patch("app.tasks.celery_tasks.send_email_task.delay")
@patch("app.tasks.celery_tasks.get_llm_client")
def test_legacy_email_only_sequence_unaffected_by_no_blueprint(mock_get_llm, mock_send_delay, sync_db):
    """No SequenceStepConfig anywhere -> falls back to the original
    FOLLOW_UP_DELAYS_DAYS email-only behavior."""
    from unittest.mock import MagicMock

    from app.schemas import ColdEmailDraft

    llm = MagicMock()
    llm.generate_follow_up_email.return_value = ColdEmailDraft(subject="Following up", body="Just checking in")
    mock_get_llm.return_value = llm

    lead = _make_sent_lead(sync_db, follow_up_step=0)

    result = follow_up_sequence_task()

    assert result["follow_ups_queued"] == 1
    mock_send_delay.assert_called_once()
    sync_db.refresh(lead)
    assert lead.follow_up_step == 1


@patch("app.services.linkedin_automation.execute_touchpoint", return_value="simulated")
def test_execute_linkedin_touchpoint_task_marks_executed(mock_execute, sync_db):
    from app.tasks.celery_tasks import execute_linkedin_touchpoint_task

    lead = _make_sent_lead(sync_db)
    touchpoint = LinkedInTouchpoint(lead_id=lead.id, sequence_step=1, action=ChannelType.LINKEDIN_VIEW)
    sync_db.add(touchpoint)
    sync_db.commit()
    sync_db.refresh(touchpoint)

    result = execute_linkedin_touchpoint_task(touchpoint.id)

    assert result["status"] == "executed"
    sync_db.refresh(touchpoint)
    assert touchpoint.status == TouchpointStatus.EXECUTED
    assert touchpoint.external_reference == "simulated"


@patch("app.tasks.celery_tasks.execute_touchpoint")
def test_execute_linkedin_touchpoint_task_marks_failed_on_automation_error(mock_execute, sync_db):
    from app.services.linkedin_automation import LinkedInAutomationError
    from app.tasks.celery_tasks import execute_linkedin_touchpoint_task

    mock_execute.side_effect = LinkedInAutomationError("no linkedin_url")

    lead = _make_sent_lead(sync_db)
    touchpoint = LinkedInTouchpoint(lead_id=lead.id, sequence_step=1, action=ChannelType.LINKEDIN_CONNECTION)
    sync_db.add(touchpoint)
    sync_db.commit()
    sync_db.refresh(touchpoint)

    result = execute_linkedin_touchpoint_task(touchpoint.id)

    assert result["status"] == "failed"
    sync_db.refresh(touchpoint)
    assert touchpoint.status == TouchpointStatus.FAILED
    assert touchpoint.error
