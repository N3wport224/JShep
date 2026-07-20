"""Prometheus metrics registry. Counters/histograms live here so any module
(LLM client, sender rotation, approvals router) can import and update them
without pulling in FastAPI - the /metrics route in app/main.py just exposes
whatever has been recorded via prometheus_client's default registry.
"""
from prometheus_client import Counter, Gauge, Histogram

email_dispatch_total = Counter(
    "sdr_email_dispatch_total",
    "Outbound email dispatch attempts",
    ["status", "sender_account"],
)

llm_tokens_total = Counter(
    "sdr_llm_tokens_total",
    "LLM tokens consumed",
    ["provider", "kind"],  # kind: input|output
)

llm_calls_total = Counter(
    "sdr_llm_calls_total",
    "LLM completion calls",
    ["provider", "operation", "outcome"],  # outcome: success|json_error|provider_error
)

queue_depth = Histogram(
    "sdr_queue_depth",
    "Observed Celery queue depth at time of sampling",
    ["queue"],
    buckets=(0, 1, 5, 10, 25, 50, 100, 250, 500, 1000),
)

approval_response_latency_seconds = Histogram(
    "sdr_approval_response_latency_seconds",
    "Time between an approval request being created and a human resolving it",
    buckets=(30, 60, 300, 900, 1800, 3600, 7200, 21600, 86400, 259200),
)

sender_bounce_rate = Histogram(
    "sdr_sender_bounce_rate",
    "Observed bounce rate per sender account at time of send",
    ["sender_account"],
    buckets=(0, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0),
)

suppression_total = Counter(
    "sdr_suppression_total",
    "Entries added to the global suppression/do-not-contact list",
    ["source"],  # opt_out_reply|unsubscribe_link|bounce|manual
)

crm_sync_total = Counter(
    "sdr_crm_sync_total",
    "CRM contact push attempts",
    ["provider", "status"],  # status: success|failed
)

variant_sent_total = Counter(
    "sdr_variant_sent_total", "Emails sent per A/B campaign variant", ["campaign", "variant"]
)
variant_open_total = Counter(
    "sdr_variant_open_total", "Opens per A/B campaign variant", ["campaign", "variant"]
)
variant_reply_total = Counter(
    "sdr_variant_reply_total", "Replies per A/B campaign variant", ["campaign", "variant"]
)
variant_positive_total = Counter(
    "sdr_variant_positive_total", "Positive-sentiment replies per A/B campaign variant", ["campaign", "variant"]
)

sender_spam_complaint_rate = Histogram(
    "sdr_sender_spam_complaint_rate",
    "Observed spam-complaint rate per sender account at time of send",
    ["sender_account"],
    buckets=(0, 0.0005, 0.001, 0.002, 0.005, 0.01, 0.05),
)

sender_warmup_stage = Gauge(
    "sdr_sender_warmup_stage",
    "Current inbox-warmup stage per sender account (higher = more ramped up)",
    ["sender_account"],
)

sender_daily_limit = Gauge(
    "sdr_sender_daily_limit",
    "Current daily send limit per sender account (ramps up during warmup)",
    ["sender_account"],
)

linkedin_touchpoint_total = Counter(
    "sdr_linkedin_touchpoint_total",
    "LinkedIn touchpoint executions handed to the automation layer",
    ["action", "status"],  # action: linkedin_view|linkedin_connection, status: executed|failed|simulated
)


def record_llm_tokens(provider: str, input_tokens: int, output_tokens: int) -> None:
    llm_tokens_total.labels(provider=provider, kind="input").inc(max(input_tokens, 0))
    llm_tokens_total.labels(provider=provider, kind="output").inc(max(output_tokens, 0))
