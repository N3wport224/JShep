"""Prometheus metrics registry. Counters/histograms live here so any module
(LLM client, sender rotation, approvals router) can import and update them
without pulling in FastAPI - the /metrics route in app/main.py just exposes
whatever has been recorded via prometheus_client's default registry.
"""
from prometheus_client import Counter, Histogram

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


def record_llm_tokens(provider: str, input_tokens: int, output_tokens: int) -> None:
    llm_tokens_total.labels(provider=provider, kind="input").inc(max(input_tokens, 0))
    llm_tokens_total.labels(provider=provider, kind="output").inc(max(output_tokens, 0))
