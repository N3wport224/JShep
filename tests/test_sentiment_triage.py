"""End-to-end (within the sync/service layer) reply triage: sentiment
classification failure handling, and that a positive reply always creates a
PENDING approval and never an already-sent message."""
from unittest.mock import MagicMock, patch

from app.models import ApprovalRequest, ApprovalStatus, Lead, LeadStatus, Reply, Sentiment
from app.schemas import ReplyDraft, SentimentClassification
from app.services import sentiment as sentiment_service


def _make_lead_and_reply(sync_db, body="Sounds great, let's talk!"):
    lead = Lead(company_name="Acme", contact_name="Jane", email="jane-triage@acme.example.com")
    sync_db.add(lead)
    sync_db.flush()
    reply = Reply(lead_id=lead.id, raw_subject="Re: intro", raw_body=body)
    sync_db.add(reply)
    sync_db.flush()
    sync_db.commit()
    return lead, reply


@patch("app.services.sentiment.notify_approval_request")
@patch("app.services.sentiment.get_llm_client")
def test_positive_reply_creates_pending_approval_never_auto_sends(mock_get_llm, mock_notify, sync_db):
    llm = MagicMock()
    llm.classify_sentiment.return_value = SentimentClassification(sentiment=Sentiment.POSITIVE, reasoning="wants call")
    llm.generate_agentic_reply.return_value = ReplyDraft(draft="Happy to chat - Tuesday work?", reasoning="ok")
    mock_get_llm.return_value = llm

    lead, reply = _make_lead_and_reply(sync_db)
    sentiment_service.triage_reply(sync_db, reply)

    assert reply.sentiment == Sentiment.POSITIVE
    assert lead.status == LeadStatus.REPLIED  # not auto-closed/won

    approval = sync_db.query(ApprovalRequest).filter_by(reply_id=reply.id).one()
    assert approval.status == ApprovalStatus.PENDING
    assert approval.drafted_response == "Happy to chat - Tuesday work?"
    mock_notify.assert_called_once()


@patch("app.services.sentiment.get_llm_client")
def test_negative_reply_opts_out_lead_no_approval(mock_get_llm, sync_db):
    llm = MagicMock()
    llm.classify_sentiment.return_value = SentimentClassification(sentiment=Sentiment.NEGATIVE, reasoning="unsubscribe")
    mock_get_llm.return_value = llm

    lead, reply = _make_lead_and_reply(sync_db, body="Remove me from your list")
    sentiment_service.triage_reply(sync_db, reply)

    assert lead.status == LeadStatus.OPTED_OUT
    assert sync_db.query(ApprovalRequest).filter_by(reply_id=reply.id).one_or_none() is None


@patch("app.services.sentiment.get_llm_client")
def test_sentiment_classification_failure_leaves_sentiment_unset_not_positive(mock_get_llm, sync_db):
    """Critical fail-safe: an LLM error must never be silently treated as
    positive (which would open the approval gate) or as any other guess."""
    llm = MagicMock()
    llm.classify_sentiment.side_effect = RuntimeError("provider 500")
    mock_get_llm.return_value = llm

    lead, reply = _make_lead_and_reply(sync_db)
    sentiment_service.triage_reply(sync_db, reply)

    assert reply.sentiment is None
    assert lead.status == LeadStatus.NEW  # untouched
    assert sync_db.query(ApprovalRequest).filter_by(reply_id=reply.id).one_or_none() is None


@patch("app.services.sentiment.notify_approval_request")
@patch("app.services.sentiment.get_llm_client")
def test_draft_generation_failure_still_creates_approval_with_placeholder(mock_get_llm, mock_notify, sync_db):
    """Even if the agentic drafting step fails, the approval gate must still
    open (positive lead shouldn't be silently dropped) - just with a
    placeholder telling the human to write the reply manually."""
    llm = MagicMock()
    llm.classify_sentiment.return_value = SentimentClassification(sentiment=Sentiment.POSITIVE, reasoning="interested")
    llm.generate_agentic_reply.side_effect = RuntimeError("provider down")
    mock_get_llm.return_value = llm

    lead, reply = _make_lead_and_reply(sync_db)
    sentiment_service.triage_reply(sync_db, reply)

    approval = sync_db.query(ApprovalRequest).filter_by(reply_id=reply.id).one()
    assert approval.status == ApprovalStatus.PENDING
    assert "failed" in approval.drafted_response.lower()


@patch("app.services.sentiment.notify_approval_request")
@patch("app.services.sentiment.get_llm_client")
def test_notification_failure_does_not_prevent_approval_persistence(mock_get_llm, mock_notify, sync_db):
    mock_notify.side_effect = RuntimeError("telegram down")
    llm = MagicMock()
    llm.classify_sentiment.return_value = SentimentClassification(sentiment=Sentiment.POSITIVE, reasoning="interested")
    llm.generate_agentic_reply.return_value = ReplyDraft(draft="Let's talk", reasoning="ok")
    mock_get_llm.return_value = llm

    lead, reply = _make_lead_and_reply(sync_db)
    sentiment_service.triage_reply(sync_db, reply)

    approval = sync_db.query(ApprovalRequest).filter_by(reply_id=reply.id).one()
    assert approval.status == ApprovalStatus.PENDING
    assert approval.notified is False
