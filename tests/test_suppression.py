"""Global suppression list: dedup, domain matching, and enforcement as a
hard gate on every outbound send path (compliance-critical - a suppressed
lead must never be contacted again, even by a stale queued task)."""
from unittest.mock import patch

from app.models import EmailMessage, Lead, LeadStatus, MessageDirection, MessageStatus, SuppressionSource
from app.services.suppression import add_to_suppression, is_suppressed, unsubscribe_footer
from app.tasks.celery_tasks import enrich_lead_task, send_email_task


def _make_lead(sync_db, email="prospect@blocked-domain.example.com"):
    lead = Lead(company_name="Acme", contact_name="Jane", email=email)
    sync_db.add(lead)
    sync_db.commit()
    sync_db.refresh(lead)
    return lead


def test_is_suppressed_false_when_no_entries(sync_db):
    assert is_suppressed(sync_db, "nobody@nowhere.example.com") is False


def test_add_and_check_exact_email_match(sync_db):
    add_to_suppression(sync_db, "opted-out@example.com", SuppressionSource.OPT_OUT_REPLY, reason="said no")
    assert is_suppressed(sync_db, "opted-out@example.com") is True
    assert is_suppressed(sync_db, "OPTED-OUT@EXAMPLE.COM") is True  # case-insensitive


def test_domain_match_blocks_other_addresses_same_domain(sync_db):
    add_to_suppression(sync_db, "someone@blocked-domain.example.com", SuppressionSource.BOUNCE)
    assert is_suppressed(sync_db, "someone-else@blocked-domain.example.com") is True


def test_add_to_suppression_is_idempotent(sync_db):
    first = add_to_suppression(sync_db, "dup@example.com", SuppressionSource.MANUAL)
    second = add_to_suppression(sync_db, "dup@example.com", SuppressionSource.MANUAL)
    assert first.id == second.id


def test_unsubscribe_footer_contains_lead_specific_link(sync_db):
    lead = _make_lead(sync_db, email="footer-test@example.com")
    footer = unsubscribe_footer(lead)
    assert lead.id in footer
    assert lead.unsubscribe_token in footer
    assert "/unsubscribe/" in footer


@patch("app.tasks.celery_tasks.get_llm_client")
def test_enrich_lead_task_skips_suppressed_lead(mock_get_llm, sync_db):
    lead = _make_lead(sync_db)
    add_to_suppression(sync_db, lead.email, SuppressionSource.OPT_OUT_REPLY)

    result = enrich_lead_task(lead.id)

    assert result["status"] == "suppressed"
    mock_get_llm.assert_not_called()  # never even calls the LLM for a suppressed lead
    sync_db.refresh(lead)
    assert lead.status == LeadStatus.OPTED_OUT


@patch("app.tasks.celery_tasks.send_via_rotation")
def test_send_email_task_skips_suppressed_lead(mock_send, sync_db):
    lead = _make_lead(sync_db, email="already-suppressed@example.com")
    add_to_suppression(sync_db, lead.email, SuppressionSource.UNSUBSCRIBE_LINK)

    message = EmailMessage(
        lead_id=lead.id,
        direction=MessageDirection.OUTBOUND,
        subject="Hi",
        body="Hello there",
        status=MessageStatus.DRAFT,
    )
    sync_db.add(message)
    sync_db.commit()
    sync_db.refresh(message)

    result = send_email_task(message.id)

    assert result["status"] == "suppressed"
    mock_send.assert_not_called()  # never touches the sender rotation manager
    sync_db.refresh(message)
    assert message.status == MessageStatus.FAILED
