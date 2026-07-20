"""LLM structured-output self-correction and failure handling."""
import pytest

from app.schemas import ColdEmailDraft, SentimentClassification
from app.services.llm import LLMClient, LLMJSONError


class _FakeLLMClient(LLMClient):
    """Bypasses provider SDK construction/network calls entirely; tests
    drive `_complete` directly via a queue of canned responses."""

    def __init__(self, responses: list[str]):
        self.settings = type("S", (), {"llm_agentic_max_steps": 3})()
        self.provider = "anthropic"
        self._responses = list(responses)
        self._calls = 0
        from app.core.resilience import get_circuit_breaker

        self._breaker = get_circuit_breaker(f"test-llm-{id(self)}")

    def _complete(self, system, user, max_tokens=1024):
        self._calls += 1
        if not self._responses:
            raise AssertionError("LLM called more times than canned responses provided")
        return self._responses.pop(0)


def test_classify_sentiment_valid_json_first_try():
    client = _FakeLLMClient(['{"sentiment": "positive_interested", "reasoning": "wants a call"}'])
    result = client.classify_sentiment("Sounds great, let's talk!")
    assert isinstance(result, SentimentClassification)
    assert result.sentiment.value == "positive_interested"
    assert client._calls == 1


def test_classify_sentiment_self_corrects_after_malformed_json():
    client = _FakeLLMClient(
        [
            "not json at all, sorry",
            '{"sentiment": "negative_opt_out", "reasoning": "explicit no"}',
        ]
    )
    result = client.classify_sentiment("Please remove me from your list")
    assert result.sentiment.value == "negative_opt_out"
    assert client._calls == 2  # first attempt failed, second self-correction succeeded


def test_classify_sentiment_self_corrects_after_invalid_enum_value():
    client = _FakeLLMClient(
        [
            '{"sentiment": "maybe_interested", "reasoning": "unclear"}',  # not a valid enum value
            '{"sentiment": "objection", "reasoning": "price concern"}',
        ]
    )
    result = client.classify_sentiment("Seems expensive")
    assert result.sentiment.value == "objection"
    assert client._calls == 2


def test_classify_sentiment_gives_up_after_max_attempts():
    client = _FakeLLMClient(["nonsense"] * 5)
    with pytest.raises(LLMJSONError):
        client.classify_sentiment("???")
    # MAX_SELF_CORRECTION_ATTEMPTS == 3
    assert client._calls == 3


def test_generate_cold_email_extracts_json_from_markdown_fence():
    client = _FakeLLMClient(
        ['```json\n{"subject": "Quick question", "body": "Hi Jane, ..."}\n```']
    )
    draft = client.generate_cold_email({"company_name": "Acme", "contact_name": "Jane"})
    assert isinstance(draft, ColdEmailDraft)
    assert draft.subject == "Quick question"


def test_generate_cold_email_rejects_missing_required_field():
    client = _FakeLLMClient(
        [
            '{"subject": "Hi"}',  # missing "body" - should fail pydantic validation and retry
            '{"subject": "Hi", "body": "Full email body here"}',
        ]
    )
    draft = client.generate_cold_email({"company_name": "Acme", "contact_name": "Jane"})
    assert draft.body == "Full email body here"
    assert client._calls == 2


def test_agentic_reply_loop_stops_when_critique_approves():
    client = _FakeLLMClient(
        [
            '{"draft": "Thanks for the interest! Free Tuesday for a call?", "reasoning": "initial"}',
            '{"approved": true, "revised_draft": null, "critique": "looks good"}',
        ]
    )
    result = client.generate_agentic_reply({"company_name": "Acme", "contact_name": "Jane"}, "(no prior thread)")
    assert "Tuesday" in result.draft
    assert client._calls == 2


def test_agentic_reply_loop_revises_when_critique_rejects():
    client = _FakeLLMClient(
        [
            '{"draft": "Sure, whenever works!", "reasoning": "initial"}',
            '{"approved": false, "revised_draft": "How about Tuesday at 2pm?", "critique": "too vague, propose a time"}',
            '{"approved": true, "revised_draft": null, "critique": "fine now"}',
        ]
    )
    result = client.generate_agentic_reply(
        {"company_name": "Acme", "contact_name": "Jane"}, "(no prior thread)", max_steps=3
    )
    assert result.draft == "How about Tuesday at 2pm?"
    assert client._calls == 3


def test_agentic_reply_loop_respects_max_steps():
    # Critique never approves - loop must still terminate at max_steps.
    responses = ['{"draft": "v0", "reasoning": "x"}']
    for i in range(1, 5):
        responses.append(f'{{"approved": false, "revised_draft": "v{i}", "critique": "keep trying"}}')
    client = _FakeLLMClient(responses)
    result = client.generate_agentic_reply({"company_name": "Acme", "contact_name": "Jane"}, "(no prior thread)", max_steps=3)
    assert result.draft == "v2"  # draft + 2 critique/revise rounds = max_steps(3) total LLM calls
    assert client._calls == 3
