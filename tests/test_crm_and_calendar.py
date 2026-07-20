"""CRM contact push and calendar-booking-link meeting-intent detection."""
from unittest.mock import MagicMock, patch

from app.models import Lead
from app.schemas import MeetingIntent
from app.services.crm import CRMSyncError, HubSpotCRM, sync_lead_to_crm
from app.services.llm import LLMClient


def _make_lead(sync_db, **overrides):
    defaults = dict(company_name="Acme", contact_name="Jane Doe", email="jane@acme.example.com")
    defaults.update(overrides)
    lead = Lead(**defaults)
    sync_db.add(lead)
    sync_db.commit()
    sync_db.refresh(lead)
    return lead


@patch("app.services.crm.get_crm_provider")
def test_sync_lead_to_crm_noop_when_unconfigured(mock_get_provider, sync_db):
    mock_get_provider.return_value = None
    lead = _make_lead(sync_db)

    sync_lead_to_crm(sync_db, lead)

    assert lead.crm_contact_id is None


@patch("app.services.crm.get_crm_provider")
def test_sync_lead_to_crm_records_contact_id_on_success(mock_get_provider, sync_db):
    provider = MagicMock()
    provider.name = "hubspot"
    provider.push_contact.return_value = "hubspot-contact-123"
    mock_get_provider.return_value = provider

    lead = _make_lead(sync_db)
    sync_lead_to_crm(sync_db, lead)

    assert lead.crm_contact_id == "hubspot-contact-123"
    assert lead.crm_synced_at is not None


@patch("app.services.crm.get_crm_provider")
def test_sync_lead_to_crm_failure_does_not_raise(mock_get_provider, sync_db):
    provider = MagicMock()
    provider.name = "hubspot"
    provider.push_contact.side_effect = CRMSyncError("HubSpot is down")
    mock_get_provider.return_value = provider

    lead = _make_lead(sync_db)
    sync_lead_to_crm(sync_db, lead)  # must not raise

    assert lead.crm_contact_id is None


def test_hubspot_extract_conflicting_id_from_409_message():
    message = "Contact already exists. Existing ID: 987654"
    assert HubSpotCRM._extract_conflicting_id(message) == "987654"


def test_hubspot_extract_conflicting_id_returns_none_when_absent():
    assert HubSpotCRM._extract_conflicting_id("some other error") is None


class _FakeLLMClient(LLMClient):
    def __init__(self, response: str):
        self.settings = type("S", (), {"llm_agentic_max_steps": 3})()
        self.provider = "anthropic"
        self._response = response
        from app.core.resilience import get_circuit_breaker

        self._breaker = get_circuit_breaker(f"test-crm-llm-{id(self)}")

    def _complete(self, system, user, max_tokens=1024):
        return self._response


def test_detect_meeting_intent_true():
    client = _FakeLLMClient('{"wants_to_book": true, "reasoning": "asked for a calendar link"}')
    result = client.detect_meeting_intent("Sure, send me your calendar so we can find a time")
    assert isinstance(result, MeetingIntent)
    assert result.wants_to_book is True


def test_detect_meeting_intent_false():
    client = _FakeLLMClient('{"wants_to_book": false, "reasoning": "just asking a question"}')
    result = client.detect_meeting_intent("What integrations do you support?")
    assert result.wants_to_book is False


def test_draft_reply_includes_booking_url_instruction_when_provided():
    client = _FakeLLMClient('{"draft": "Book here: https://cal.com/acme/intro", "reasoning": "included link"}')
    draft = client.draft_reply(
        {"contact_name": "Jane", "company_name": "Acme"}, "(thread)", booking_url="https://cal.com/acme/intro"
    )
    assert "cal.com" in draft.draft
