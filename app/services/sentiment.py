"""
Sentiment triage pipeline: classifies an inbound Reply, and for
Positive/Interested replies, creates an ApprovalRequest and fires a
human-in-the-loop notification. This is the ONLY path that can produce an
outbound reply to a prospect, and it always stops at the approval gate.
"""
import logging

from sqlalchemy.orm import Session

from app.models import ApprovalRequest, Lead, LeadStatus, Reply, Sentiment
from app.services.llm import get_llm_client
from app.services.notifier import notify_approval_request

logger = logging.getLogger(__name__)


def triage_reply(db: Session, reply: Reply) -> Reply:
    lead: Lead = reply.lead
    llm = get_llm_client()

    try:
        sentiment_value = llm.classify_sentiment(reply.raw_body)
    except Exception as exc:  # noqa: BLE001 - malformed JSON, rate limits, or provider errors
        logger.error("Sentiment classification failed for reply %s: %s", reply.id, exc)
        # Fail safe: leave sentiment unset rather than guessing, so it can be
        # retried or triaged manually. Never assume positive on failure.
        db.commit()
        return reply

    reply.sentiment = Sentiment(sentiment_value)

    if reply.sentiment == Sentiment.NEGATIVE:
        lead.status = LeadStatus.OPTED_OUT
    elif reply.sentiment == Sentiment.OBJECTION:
        lead.status = LeadStatus.REPLIED
    elif reply.sentiment == Sentiment.POSITIVE:
        lead.status = LeadStatus.REPLIED
        _open_approval_gate(db, lead, reply)

    db.commit()
    db.refresh(reply)
    return reply


def _open_approval_gate(db: Session, lead: Lead, reply: Reply) -> ApprovalRequest:
    """CRITICAL: this only ever creates a PENDING approval request and
    notifies a human. It must never send an email itself."""
    llm = get_llm_client()
    lead_dict = {
        "company_name": lead.company_name,
        "contact_name": lead.contact_name,
        "website": lead.website,
        "linkedin_url": lead.linkedin_url,
    }
    try:
        draft = llm.draft_reply(lead_dict, reply.raw_body)
    except Exception as exc:  # noqa: BLE001 - malformed JSON, rate limits, or provider errors
        logger.error("Draft reply generation failed for lead %s: %s", lead.id, exc)
        draft = (
            "[AI draft generation failed - please write a manual reply before approving.]"
        )

    approval = ApprovalRequest(lead_id=lead.id, reply_id=reply.id, drafted_response=draft)
    db.add(approval)
    db.flush()

    try:
        notify_approval_request(approval, lead, reply)
        approval.notified = True
    except Exception as exc:  # notification failures must not block persistence
        logger.error("Failed to send human-in-the-loop notification for approval %s: %s", approval.id, exc)

    db.flush()
    return approval
