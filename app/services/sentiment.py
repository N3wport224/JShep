"""
Sentiment triage pipeline: classifies an inbound Reply, and for
Positive/Interested replies, runs a multi-step agentic drafting loop (using
the lead's full conversation memory) to produce a proposed response, then
creates an ApprovalRequest and fires a human-in-the-loop notification. This
is the ONLY path that can produce an outbound reply to a prospect, and it
always stops at the approval gate.

Also the integration point for two of the enterprise modules: a
Negative/Opt-Out reply adds the lead to the global suppression list (see
app.services.suppression), and a Positive/Interested reply pushes the lead
to the configured CRM (see app.services.crm) and, if the prospect asked to
schedule a call, hands the agentic drafting loop a calendar booking link.
"""
import logging

from sqlalchemy.orm import Session

from app.models import ApprovalRequest, Lead, LeadStatus, Reply, Sentiment, SuppressionSource
from app.services.calendar import get_booking_link
from app.services.campaigns import record_positive, record_reply
from app.services.crm import sync_lead_to_crm
from app.services.llm import get_llm_client
from app.services.memory import build_thread_context, record_inbound
from app.services.notifier import notify_approval_request
from app.services.suppression import add_to_suppression

logger = logging.getLogger(__name__)


def triage_reply(db: Session, reply: Reply) -> Reply:
    lead: Lead = reply.lead
    llm = get_llm_client()

    record_inbound(db, lead, reply.raw_subject, reply.raw_body, source_reply_id=reply.id)
    record_reply(db, lead)

    try:
        classification = llm.classify_sentiment(reply.raw_body)
    except Exception as exc:  # noqa: BLE001 - malformed JSON, rate limits, or provider errors
        logger.error("Sentiment classification failed for reply %s: %s", reply.id, exc)
        # Fail safe: leave sentiment unset rather than guessing, so it can be
        # retried or triaged manually. Never assume positive on failure.
        db.commit()
        return reply

    reply.sentiment = classification.sentiment

    if reply.sentiment == Sentiment.NEGATIVE:
        lead.status = LeadStatus.OPTED_OUT
        add_to_suppression(
            db, lead.email, SuppressionSource.OPT_OUT_REPLY, reason=f"Negative reply: {reply.raw_body[:200]!r}"
        )
    elif reply.sentiment == Sentiment.OBJECTION:
        lead.status = LeadStatus.REPLIED
    elif reply.sentiment == Sentiment.POSITIVE:
        lead.status = LeadStatus.REPLIED
        record_positive(db, lead)
        sync_lead_to_crm(db, lead)
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
    thread_context = build_thread_context(db, lead)

    booking_url = None
    try:
        intent = llm.detect_meeting_intent(reply.raw_body)
        if intent.wants_to_book:
            booking_url = get_booking_link()
    except Exception as exc:  # noqa: BLE001 - meeting-intent detection is best-effort
        logger.warning("Meeting intent detection failed for lead %s: %s", lead.id, exc)

    try:
        agentic_draft = llm.generate_agentic_reply(lead_dict, thread_context, booking_url=booking_url)
        draft = agentic_draft.draft
    except Exception as exc:  # noqa: BLE001 - malformed JSON, rate limits, or provider errors
        logger.error("Agentic draft reply generation failed for lead %s: %s", lead.id, exc)
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
