"""
LLM orchestration layer. Wraps whichever provider is configured
(Anthropic or OpenAI) behind a single interface used by the rest of the
app for: cold email generation, reply sentiment classification, and
draft-reply generation for the human-in-the-loop approval gate.

All calls that expect structured output ask the model for JSON and are
wrapped in retry logic that tolerates rate limits and malformed JSON.
"""
import json
import logging
from typing import Literal

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import get_settings
from app.models import Sentiment

logger = logging.getLogger(__name__)

SENTIMENT_VALUES = [s.value for s in Sentiment]


class LLMJSONError(Exception):
    """Raised when the LLM response could not be parsed as valid JSON after retries."""


class LLMRateLimitError(Exception):
    """Raised when the upstream provider signals a rate limit that retries could not resolve."""


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

    @retry(
        reraise=True,
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((LLMRateLimitError, ConnectionError)),
    )
    def _complete(self, system: str, user: str, max_tokens: int = 1024) -> str:
        try:
            if self.provider == "anthropic":
                response = self._client.messages.create(
                    model=self.settings.anthropic_model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                )
                return response.content[0].text
            else:
                response = self._client.chat.completions.create(
                    model=self.settings.openai_model,
                    max_tokens=max_tokens,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                )
                return response.choices[0].message.content
        except Exception as exc:  # provider SDKs raise their own rate-limit types
            message = str(exc).lower()
            if "rate limit" in message or "429" in message or "overloaded" in message:
                raise LLMRateLimitError(str(exc)) from exc
            raise

    def _complete_json(self, system: str, user: str, max_tokens: int = 1024) -> dict:
        last_error: Exception | None = None
        for attempt in range(3):
            raw = self._complete(system, user, max_tokens=max_tokens)
            try:
                return _extract_json(raw)
            except LLMJSONError as exc:
                last_error = exc
                logger.warning("LLM JSON parse failed (attempt %s/3): %s", attempt + 1, exc)
                user = (
                    f"{user}\n\nYour previous reply was not valid JSON. "
                    "Respond with ONLY a single valid JSON object, no markdown, no commentary."
                )
        raise last_error or LLMJSONError("LLM did not return valid JSON")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_cold_email(self, lead: dict) -> dict:
        """Generate a personalized cold email for a lead.

        `lead` is a dict with company_name, contact_name, website, linkedin_url.
        Returns {"subject": str, "body": str}.
        """
        system = (
            "You are an expert SDR copywriter. Write concise, highly personalized "
            "B2B cold outreach emails. Never use generic templates or filler. "
            "Reference specifics about the recipient's company where plausible. "
            "Keep the body under 150 words, no markdown, plain text with line breaks. "
            "Respond with ONLY a JSON object: {\"subject\": string, \"body\": string}."
        )
        user = (
            "Write a cold outreach email for this lead:\n"
            f"Company: {lead.get('company_name')}\n"
            f"Contact: {lead.get('contact_name')}\n"
            f"Website: {lead.get('website') or 'unknown'}\n"
            f"LinkedIn: {lead.get('linkedin_url') or 'unknown'}\n"
        )
        data = self._complete_json(system, user)
        if "subject" not in data or "body" not in data:
            raise LLMJSONError(f"LLM cold email JSON missing required keys: {data}")
        return data

    def generate_follow_up_email(self, lead: dict, step: int, previous_body: str) -> dict:
        system = (
            "You are an expert SDR copywriter writing a brief, friendly follow-up "
            "to a cold email that received no reply. Do not repeat the first email "
            "verbatim; add a new angle or piece of value. Keep it under 80 words. "
            "Respond with ONLY a JSON object: {\"subject\": string, \"body\": string}."
        )
        user = (
            f"This is follow-up #{step} to {lead.get('contact_name')} at {lead.get('company_name')}.\n"
            f"Original email body:\n{previous_body}\n"
        )
        data = self._complete_json(system, user)
        if "subject" not in data or "body" not in data:
            raise LLMJSONError(f"LLM follow-up email JSON missing required keys: {data}")
        return data

    def classify_sentiment(self, reply_text: str) -> Literal["positive_interested", "objection", "negative_opt_out"]:
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
        data = self._complete_json(system, user, max_tokens=256)
        sentiment = data.get("sentiment")
        if sentiment not in SENTIMENT_VALUES:
            raise LLMJSONError(f"LLM returned invalid sentiment value: {sentiment!r}")
        return sentiment

    def draft_reply(self, lead: dict, reply_text: str) -> str:
        """Draft a proposed response to a positive/interested reply.
        This draft is NEVER sent automatically - it is only shown to a human
        in the approval gate."""
        system = (
            "You are an SDR assistant drafting a reply to a prospect who responded "
            "positively to cold outreach. Be warm, concise, and propose concrete next "
            "steps (e.g. a call). Under 100 words, plain text. "
            "Respond with ONLY a JSON object: {\"draft\": string}."
        )
        user = (
            f"Prospect: {lead.get('contact_name')} at {lead.get('company_name')}\n"
            f"Their reply:\n{reply_text}\n"
        )
        data = self._complete_json(system, user)
        draft = data.get("draft")
        if not draft:
            raise LLMJSONError(f"LLM draft reply JSON missing 'draft' key: {data}")
        return draft


_llm_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client
