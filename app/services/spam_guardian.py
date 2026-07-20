"""
Pre-send spam heuristic guardian. A deterministic, dependency-free scoring
pass - no LLM call, no external API - run on every cold email and
follow-up BEFORE it can enter the outbound sending queue (see
app.tasks.celery_tasks.enrich_lead_task / _send_email_step). Checks for
aggressive trigger words, spammy punctuation/formatting (ALL CAPS, "!!!"),
and link density.

This complements, not replaces, real-world spam filters and the sender
health guardian (app.services.sender_rotation) - it catches the most
common, mechanically-detectable red flags in the copy itself, before
reputation-based signals like bounce/spam-complaint rate would ever kick in.

Score >= SPAM_SCORE_REWRITE_THRESHOLD triggers LLM self-correction rewrite
attempts (run_with_guard, called from the Celery tasks with the actual
generate function); if still >= SPAM_SCORE_FLAG_THRESHOLD after those
attempts, the caller holds the message as NEEDS_REVIEW instead of queueing
it for automatic sending.
"""
import re
from typing import Callable, Protocol, TypeVar

from app.config import get_settings
from app.schemas import SpamRiskLevel, SpamScoreResult

# Curated, deliberately conservative list of B2B cold-email spam signals -
# words/phrases that are heavily weighted by real spam filters and rarely
# belong in genuine, conversational sales copy.
TRIGGER_WORDS = [
    "act now",
    "act immediately",
    "apply now",
    "buy now",
    "cash bonus",
    "cancel at any time",
    "click here",
    "congratulations",
    "dear friend",
    "double your",
    "earn extra cash",
    "earn money",
    "extra income",
    "guaranteed",
    "increase sales",
    "limited time",
    "lowest price",
    "no credit check",
    "no obligation",
    "no cost",
    "once in a lifetime",
    "order now",
    "risk-free",
    "this is not spam",
    "urgent",
    "while supplies last",
    "winner",
    "work from home",
]

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_EXCESSIVE_PUNCT_RE = re.compile(r"[!?]{2,}")
_WORD_RE = re.compile(r"[A-Za-z']+")
_MONEY_URGENCY_RE = re.compile(r"\${2,}|\bfree\b[^.!?]{0,20}\bnow\b", re.IGNORECASE)


def score_email(subject: str, body: str) -> SpamScoreResult:
    """Score a single email 0-100 (higher = spammier) against a handful of
    mechanically-detectable heuristics. Deterministic and side-effect free."""
    settings = get_settings()
    subject = subject or ""
    body = body or ""
    combined_lower = f"{subject}\n{body}".lower()
    reasons: list[str] = []
    score = 0

    found_words = sorted(
        {w for w in TRIGGER_WORDS if re.search(rf"\b{re.escape(w)}\b", combined_lower)}
    )
    if found_words:
        points = min(len(found_words) * 8, 40)
        score += points
        reasons.append(f"{len(found_words)} spam trigger phrase(s): {', '.join(found_words)}")

    punct_hits = _EXCESSIVE_PUNCT_RE.findall(subject) + _EXCESSIVE_PUNCT_RE.findall(body)
    if punct_hits:
        points = min(len(punct_hits) * 10, 20)
        score += points
        reasons.append(f"{len(punct_hits)} run(s) of excessive punctuation (e.g. '!!!', '??')")

    body_words = _WORD_RE.findall(body)
    caps_words = [w for w in body_words if len(w) >= 4 and w.isupper()]
    caps_ratio = (len(caps_words) / len(body_words)) if body_words else 0.0
    if caps_ratio > 0.15:
        score += 20
        reasons.append(f"{caps_ratio:.0%} of body words are ALL CAPS")

    if subject and subject.isupper() and len(subject) > 3:
        score += 15
        reasons.append("subject line is entirely uppercase")

    links = _URL_RE.findall(body)
    word_count = max(len(body_words), 1)
    link_density = len(links) / word_count
    if len(links) > 2 or link_density > 0.04:
        points = min(len(links) * 10, 30)
        score += points
        reasons.append(f"{len(links)} link(s) in a {word_count}-word email (high link density)")

    if _MONEY_URGENCY_RE.search(combined_lower):
        score += 10
        reasons.append("aggressive money/urgency phrasing (e.g. '$$$', 'free ... now')")

    score = min(score, 100)
    if score >= settings.spam_score_flag_threshold:
        risk_level = SpamRiskLevel.HIGH
    elif score >= settings.spam_score_rewrite_threshold:
        risk_level = SpamRiskLevel.MEDIUM
    else:
        risk_level = SpamRiskLevel.LOW

    return SpamScoreResult(
        score=score,
        risk_level=risk_level,
        trigger_words_found=found_words,
        caps_ratio=round(caps_ratio, 3),
        link_count=len(links),
        link_density=round(link_density, 4),
        reasons=reasons,
    )


class _HasSubjectBody(Protocol):
    subject: str
    body: str


DraftT = TypeVar("DraftT", bound=_HasSubjectBody)


def run_with_guard(
    generate: Callable[[str | None], DraftT], max_attempts: int | None = None
) -> tuple[DraftT, SpamScoreResult]:
    """Generate a draft, score it, and - while still HIGH risk - re-invoke
    `generate` with the scoring feedback as a rewrite instruction, up to
    `max_attempts` times (default SPAM_GUARDIAN_MAX_REWRITE_ATTEMPTS).
    `generate(feedback)` is called with feedback=None on the first attempt
    and the joined reasons string on each retry. Returns the final draft and
    its score - the caller decides what to do with a still-HIGH result
    (hold for manual review) based on `result.risk_level`."""
    settings = get_settings()
    max_attempts = settings.spam_guardian_max_rewrite_attempts if max_attempts is None else max_attempts

    draft = generate(None)
    result = score_email(draft.subject, draft.body)

    attempt = 0
    while result.risk_level == SpamRiskLevel.HIGH and attempt < max_attempts:
        feedback = "; ".join(result.reasons)
        draft = generate(feedback)
        result = score_email(draft.subject, draft.body)
        attempt += 1

    return draft, result
