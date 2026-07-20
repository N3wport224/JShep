"""
LLM orchestration layer. Wraps whichever provider is configured
(Anthropic or OpenAI) behind a single interface used by the rest of the
app for: cold email generation, reply sentiment classification, and
agentic draft-reply generation for the human-in-the-loop approval gate.

Every structured call validates its JSON response against a Pydantic model
(app.schemas) and, on a validation failure, retries with the validation
error fed back to the model as automated self-correction - separate from
the tenacity-driven retry/circuit-breaker layer that handles transport
failures and rate limits.
"""
import json
import logging
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.config import get_settings
from app.core.metrics import llm_calls_total, record_llm_tokens
from app.core.resilience import RateLimitedError, get_circuit_breaker, resilient_retry
from app.schemas import ColdEmailDraft, MeetingIntent, ReplyCritique, ReplyDraft, SentimentClassification

logger = logging.getLogger(__name__)

SchemaT = TypeVar("SchemaT", bound=BaseModel)

MAX_SELF_CORRECTION_ATTEMPTS = 3


class LLMJSONError(Exception):
    """Raised when the LLM response could not be parsed/validated as the
    expected structured output after all self-correction retries."""


def _extract_json(text: str) -> dict:
    """Best-effort extraction of a JSON object from an LLM response that may
    include markdown fences or leading/trailing prose."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise LLMJSONError(f"No JSON object found in LLM response: {text[:200]!r}")
    candidate = text[start : end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise LLMJSONError(f"Could not decode JSON from LLM response: {exc}") from exc


class LLMClient:
    def __init__(self):
        self.settings = get_settings()
        self.provider = self.settings.llm_provider
        self._breaker = get_circuit_breaker(f"llm:{self.provider}", failure_threshold=5, recovery_timeout_seconds=30)
        if self.provider == "anthropic":
            if not self.settings.anthropic_api_key:
                logger.warning("ANTHROPIC_API_KEY is not set; LLM calls will fail until configured.")
            import anthropic

            self._client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key)
        elif self.provider == "openai":
            if not self.settings.openai_api_key:
                logger.warning("OPENAI_API_KEY is not set; LLM calls will fail until configured.")
            import openai

            self._client = openai.OpenAI(api_key=self.settings.openai_api_key)
        else:
            raise ValueError(f"Unsupported LLM provider: {self.provider}")

    @resilient_retry(retryable_exceptions=(RateLimitedError, ConnectionError, TimeoutError))
    def _complete(self, system: str, user: str, max_tokens: int = 1024) -> str:
        self._breaker.before_call()
        try:
            if self.provider == "anthropic":
                response = self._client.messages.create(
                    model=self.settings.anthropic_model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                )
                text = response.content[0].text
                record_llm_tokens(
                    self.provider, response.usage.input_tokens, response.usage.output_tokens
                )
            else:
                response = self._client.chat.completions.create(
                    model=self.settings.openai_model,
                    max_tokens=max_tokens,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                )
                text = response.choices[0].message.content
                usage = response.usage
                record_llm_tokens(
                    self.provider,
                    getattr(usage, "prompt_tokens", 0) or 0,
                    getattr(usage, "completion_tokens", 0) or 0,
                )
        except Exception as exc:  # provider SDKs raise their own rate-limit types
            self._breaker.on_failure()
            message = str(exc).lower()
            if "rate limit" in message or "429" in message or "overloaded" in message:
                raise RateLimitedError(str(exc)) from exc
            raise
        else:
            self._breaker.on_success()
            return text

    def _complete_structured(
        self, system: str, user: str, schema: type[SchemaT], operation: str, max_tokens: int = 1024
    ) -> SchemaT:
        """Call the LLM and validate the response against `schema`. On a JSON
        or Pydantic validation failure, feeds the exact error back to the
        model and retries (self-correction) up to MAX_SELF_CORRECTION_ATTEMPTS
        times before giving up."""
        last_error: Exception | None = None
        current_user = user
        for attempt in range(MAX_SELF_CORRECTION_ATTEMPTS):
            raw = self._complete(system, current_user, max_tokens=max_tokens)
            try:
                data = _extract_json(raw)
                validated = schema.model_validate(data)
            except (LLMJSONError, ValidationError) as exc:
                last_error = exc
                logger.warning(
                    "LLM structured output failed validation for %s (attempt %s/%s): %s",
                    operation,
                    attempt + 1,
                    MAX_SELF_CORRECTION_ATTEMPTS,
                    exc,
                )
                llm_calls_total.labels(provider=self.provider, operation=operation, outcome="json_error").inc()
                current_user = (
                    f"{user}\n\nYour previous response was invalid: {exc}\n"
                    f"Respond with ONLY a single valid JSON object matching this schema: "
                    f"{schema.model_json_schema()}"
                )
                continue
            else:
                llm_calls_total.labels(provider=self.provider, operation=operation, outcome="success").inc()
                return validated

        llm_calls_total.labels(provider=self.provider, operation=operation, outcome="provider_error").inc()
        raise LLMJSONError(
            f"LLM did not return valid {schema.__name__} after {MAX_SELF_CORRECTION_ATTEMPTS} attempts: {last_error}"
        )

    # ------------------------------------------------------------------
    # Single-shot structured calls
    # ------------------------------------------------------------------

    def generate_cold_email(
        self, lead: dict, variant_hint: str | None = None, spam_feedback: str | None = None
    ) -> ColdEmailDraft:
        """Generate a personalized cold email for a lead.
        `lead` is a dict with company_name, contact_name, website, linkedin_url.
        `variant_hint` (optional) is an A/B campaign variant's prompt_hint -
        e.g. a requested subject-line style or angle - so each variant
        produces a distinguishable email rather than converging on the same
        copy every time. `spam_feedback` (optional) is
        app.services.spam_guardian's flagged reasons from a previous attempt -
        when set, this is a self-correction rewrite, not a fresh draft."""
        system = (
            "You are an expert SDR copywriter. Write concise, highly personalized "
            "B2B cold outreach emails. Never use generic templates or filler. "
            "Reference specifics about the recipient's company where plausible. "
            "Keep the body under 150 words, no markdown, plain text with line breaks. "
            "Avoid aggressive sales/spam language (e.g. \"act now\", \"guaranteed\", "
            "excessive exclamation points, ALL CAPS, or more than one link) - write "
            "like a real person, not a marketing blast. "
            "Respond with ONLY a JSON object: {\"subject\": string, \"body\": string}."
        )
        user = (
            "Write a cold outreach email for this lead:\n"
            f"Company: {lead.get('company_name')}\n"
            f"Contact: {lead.get('contact_name')}\n"
            f"Website: {lead.get('website') or 'unknown'}\n"
            f"LinkedIn: {lead.get('linkedin_url') or 'unknown'}\n"
        )
        if variant_hint:
            user += f"\nA/B test variant instruction - follow this angle/style: {variant_hint}\n"
        if spam_feedback:
            user += (
                f"\nYour previous draft was flagged by our pre-send spam heuristic checker "
                f"for: {spam_feedback}. Rewrite it to sound more natural and conversational, "
                f"remove any unnecessary links, and tone down urgency/sales language.\n"
            )
        return self._complete_structured(system, user, ColdEmailDraft, operation="generate_cold_email")

    def generate_follow_up_email(
        self, lead: dict, step: int, previous_body: str, spam_feedback: str | None = None
    ) -> ColdEmailDraft:
        system = (
            "You are an expert SDR copywriter writing a brief, friendly follow-up "
            "to a cold email that received no reply. Do not repeat the first email "
            "verbatim; add a new angle or piece of value. Keep it under 80 words. "
            "Avoid aggressive sales/spam language (e.g. \"act now\", \"guaranteed\", "
            "excessive exclamation points, ALL CAPS, or more than one link) - write "
            "like a real person, not a marketing blast. "
            "Respond with ONLY a JSON object: {\"subject\": string, \"body\": string}."
        )
        user = (
            f"This is follow-up #{step} to {lead.get('contact_name')} at {lead.get('company_name')}.\n"
            f"Original email body:\n{previous_body}\n"
        )
        if spam_feedback:
            user += (
                f"\nYour previous draft was flagged by our pre-send spam heuristic checker "
                f"for: {spam_feedback}. Rewrite it to sound more natural and conversational, "
                f"remove any unnecessary links, and tone down urgency/sales language.\n"
            )
        return self._complete_structured(system, user, ColdEmailDraft, operation="generate_follow_up_email")

    def classify_sentiment(self, reply_text: str) -> SentimentClassification:
        system = (
            "You classify inbound sales email replies into exactly one of three "
            "categories: positive_interested, objection, negative_opt_out. "
            "positive_interested = wants to talk / interested / asking for a meeting. "
            "objection = raises a concern, asks a clarifying question, price pushback, "
            "not now but not a hard no. "
            "negative_opt_out = explicit no, unsubscribe, hostile, do-not-contact. "
            "Respond with ONLY a JSON object: {\"sentiment\": one of the three values, "
            "\"reasoning\": short string}."
        )
        user = f"Classify this reply:\n\n{reply_text}"
        return self._complete_structured(system, user, SentimentClassification, operation="classify_sentiment", max_tokens=256)

    def detect_meeting_intent(self, reply_text: str) -> MeetingIntent:
        """Does this reply ask to schedule/book a call or meeting? Used to
        decide whether to hand the agentic drafting loop a calendar booking
        link (app.services.calendar) to include in its response."""
        system = (
            "You determine whether an inbound sales reply is asking to schedule "
            "a call, demo, or meeting (e.g. \"can we hop on a call\", \"send me your "
            "calendar\", \"when are you free\"). Respond with ONLY a JSON object: "
            "{\"wants_to_book\": bool, \"reasoning\": short string}."
        )
        user = f"Reply:\n\n{reply_text}"
        return self._complete_structured(system, user, MeetingIntent, operation="detect_meeting_intent", max_tokens=200)

    # ------------------------------------------------------------------
    # Agentic reply drafting (multi-step: draft -> self-critique -> finalize)
    # See generate_agentic_reply below for the full loop with thread memory.
    # ------------------------------------------------------------------

    def draft_reply(self, lead: dict, thread_context: str, booking_url: str | None = None) -> ReplyDraft:
        """Draft a proposed response to a positive/interested reply, given
        the full thread context (not just the latest message). This draft is
        NEVER sent automatically - it is only ever shown to a human in the
        approval gate."""
        system = (
            "You are an SDR assistant drafting a reply to a prospect who responded "
            "positively to cold outreach. Read the full conversation thread below "
            "(oldest first) and write a warm, concise reply that acknowledges prior "
            "context and proposes concrete next steps (e.g. a call). Under 100 words, "
            "plain text. Respond with ONLY a JSON object: "
            "{\"draft\": string, \"reasoning\": string}."
        )
        user = (
            f"Prospect: {lead.get('contact_name')} at {lead.get('company_name')}\n\n"
            f"Conversation thread so far:\n{thread_context}\n"
        )
        if booking_url:
            user += (
                f"\nThe prospect wants to schedule a call/meeting. Include this "
                f"booking link naturally in the reply so they can pick a time "
                f"themselves: {booking_url}\n"
            )
        return self._complete_structured(system, user, ReplyDraft, operation="draft_reply")

    def critique_reply(self, lead: dict, thread_context: str, draft: str, booking_url: str | None = None) -> ReplyCritique:
        """Second agentic step: have the model critique its own draft against
        the full thread before it's shown to a human, catching things like
        repeating a question the prospect already answered."""
        system = (
            "You are a meticulous SDR reply reviewer. Given a conversation thread and "
            "a drafted reply, check for: repeating something already said, ignoring a "
            "question the prospect asked, over-promising, or being too pushy. If the "
            "draft is fine, approve it. If not, provide a revised draft. Respond with "
            "ONLY a JSON object: {\"approved\": bool, \"revised_draft\": string or null, "
            "\"critique\": string}."
        )
        user = (
            f"Prospect: {lead.get('contact_name')} at {lead.get('company_name')}\n\n"
            f"Conversation thread so far:\n{thread_context}\n\n"
            f"Drafted reply to review:\n{draft}\n"
        )
        if booking_url:
            user += (
                f"\nThe prospect wants to schedule a call - verify the draft includes "
                f"this booking link, and add it if missing: {booking_url}\n"
            )
        return self._complete_structured(system, user, ReplyCritique, operation="critique_reply")

    def generate_agentic_reply(
        self, lead: dict, thread_context: str, max_steps: int | None = None, booking_url: str | None = None
    ) -> ReplyDraft:
        """Multi-step agentic loop: draft, then repeatedly self-critique and
        revise until the critique step approves or max_steps is reached.
        Always returns a ReplyDraft - the caller still routes it through the
        human approval gate before anything is sent. `booking_url` is passed
        through when app.services.llm.detect_meeting_intent found the
        prospect asking to schedule a call."""
        max_steps = max_steps or self.settings.llm_agentic_max_steps
        draft = self.draft_reply(lead, thread_context, booking_url=booking_url)

        for step in range(max_steps - 1):
            critique = self.critique_reply(lead, thread_context, draft.draft, booking_url=booking_url)
            if critique.approved or not critique.revised_draft:
                break
            logger.info("Agentic reply loop step %d: revising draft (%s)", step + 1, critique.critique)
            draft = ReplyDraft(draft=critique.revised_draft, reasoning=critique.critique)

        return draft


_llm_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client
