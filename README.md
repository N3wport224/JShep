# AI SDR Backend

A containerized, modular, production-grade AI Sales Development
Representative (SDR) backend. It automates outbound prospecting and reply
sentiment triage with an agentic drafting loop, but **never** sends an
AI-drafted reply without explicit human approval.

## Architecture

The web process is intentionally thin: async request handling + Celery task
enqueueing only. All LLM/SMTP/IMAP work happens in Celery workers, never
inline on a request.

```
                        ┌─────────────┐
   HTTP/webhooks ─────▶ │  FastAPI    │──────▶ Postgres (async, asyncpg)
                        │  (uvicorn)  │
                        └──────┬──────┘
                               │ enqueue (Celery .apply_async)
                               ▼
                        ┌─────────────┐        ┌─────────────┐
                        │ Redis       │◀──────▶│ Celery beat │ (poll IMAP,
                        │ (broker)    │        │ (scheduler) │  follow-ups,
                        └──────┬──────┘        └─────────────┘  bounce check)
                               │
                               ▼
                        ┌─────────────┐
                        │ Celery      │──────▶ Postgres (sync, psycopg2)
                        │ worker(s)   │──────▶ LLM (Anthropic/OpenAI)
                        │             │──────▶ SMTP / Instantly / Smartlead
                        └─────────────┘──────▶ IMAP
```

```
app/
  main.py                FastAPI app assembly: middleware, routers, /metrics
  celery_app.py            Celery app + beat schedule (replaces APScheduler)
  config.py                  Settings (env vars)
  database.py                 Async SQLAlchemy engine/session (web layer)
  db_sync.py                   Sync SQLAlchemy engine/session (workers/Alembic)
  models.py                     Lead, EmailMessage, Reply, ApprovalRequest,
                                  ThreadMessage (conversation memory), SenderAccount
  schemas.py                     Pydantic request/response + LLM structured-output models
  core/
    logging.py                    structlog JSON logging + request-ID propagation
    metrics.py                     Prometheus counters/histograms
    resilience.py                  Circuit breaker + backoff/jitter retry preset
    security.py                    API key / JWT auth, slowapi rate limiter
  routers/
    leads.py                       CSV/JSON ingestion, enrichment (queues a task)
    outbound.py                     Queue a drafted email for dispatch
    tracking.py                      Open-tracking pixel + bounce reporting
    inbound.py                        IMAP poll trigger + generic reply webhook
    approvals.py                       The human-in-the-loop approval gate API
    telegram.py                         Telegram inline-button callback receiver
    dashboard.py                         Admin-authenticated HTML approval dashboard
    auth.py                              JWT issuance (POST /auth/token)
  services/
    llm.py                    Anthropic/OpenAI wrapper: structured output validation
                                with self-correction retries, agentic reply loop
    email_sender.py             Low-level SMTP dispatch (used by sender_rotation)
    sender_rotation.py            Multi-account sender pool, provider abstraction
                                    (SMTP/Instantly/Smartlead), bounce/health tracking
    imap_listener.py                IMAP polling + reply matching + body parsing
    sentiment.py                      Sentiment triage -> agentic draft -> approval gate
    memory.py                          Per-lead conversation memory (ThreadMessage)
    notifier.py                         Telegram / Discord / generic webhook fan-out
  tasks/
    celery_tasks.py            All background task bodies (enrich, send, poll, triage,
                                 follow-up sequence, bounce health check)
    dispatch.py                  enqueue() / request-ID propagation into tasks
alembic/                 Database migrations (source of truth for schema)
tests/                    pytest suite (LLM self-correction, IMAP parsing quirks,
                            approval race conditions, circuit breaker, bounce auto-pause)
```

### Request/task flow

1. **Ingest** leads via CSV or JSON (`POST /leads/upload`, `/leads/upload/file`).
2. **Enrich**: `POST /leads/{id}/enrich` (or `/leads/enrich/batch` for many at
   once) queues `enrich_lead_task` - the LLM call happens in a worker, not on
   the request. Response is `202 {task_id}`.
3. **Send**: `POST /outbound/send/{message_id}` queues `send_email_task`,
   which picks a healthy account via the sender rotation manager, sends, and
   records the message in the lead's conversation memory. Celery beat's
   `follow-up-sequence` task advances the follow-up sequence automatically.
4. **Replies** arrive via Celery beat's `poll-inbox` task (IMAP) or
   `POST /inbound/webhook` (pushed from an outreach platform). Both enqueue
   `triage_reply_task`.
5. Triage classifies sentiment (`positive_interested` / `objection` /
   `negative_opt_out`) via a Pydantic-validated LLM call with automatic
   self-correction retries on malformed JSON.
6. **`positive_interested` replies never auto-reply.** The system runs a
   multi-step agentic loop (draft -> self-critique -> revise, using the
   lead's *entire* conversation thread as context, not just the latest
   message) and creates a `PENDING` `ApprovalRequest`, then pushes an
   interactive notification (Telegram inline buttons, and/or a Discord/
   generic webhook message with Approve/Reject links) containing the
   prospect's message and the AI draft.
7. A human resolves the decision via `GET/POST /approvals/{id}/decision`
   (or the Telegram/Discord button/link). This transition is a single
   **atomic conditional UPDATE** (`WHERE status = 'pending'`), so two
   concurrent clicks can't both succeed - the loser gets `409`. Only after
   that commits does `send_approved_reply_task` get queued to actually send.

## Resilience

- **Circuit breakers** (`app.core.resilience.CircuitBreaker`) wrap every
  outbound integration (SMTP per-account, IMAP, LLM provider, Instantly/
  Smartlead APIs) - after repeated failures a breaker opens and fails fast
  for a recovery window instead of hammering a down provider.
- **Exponential backoff + jitter** (tenacity, `wait_exponential_jitter`)
  retries transient failures and HTTP 429s.
- **Sender rotation**: `app.services.sender_rotation` pools multiple SMTP
  accounts (or Instantly/Smartlead API keys) and picks a healthy,
  non-paused, under-daily-limit account per send (least-recently-used).
- **Bounce/health tracking**: each `SenderAccount` tracks `sent_count` /
  `bounce_count`; once the bounce rate crosses `BOUNCE_RATE_PAUSE_THRESHOLD`
  (after `BOUNCE_RATE_MIN_SAMPLE` sends), the account is auto-paused and
  excluded from rotation. `POST /tracking/bounce/{message_id}` is the
  integration point for a provider's bounce/DSN webhook.

## Observability

- **Structured JSON logs** (structlog) - every log line, including plain
  `logging.getLogger(...)` calls throughout the codebase, gets the same JSON
  shape and a `request_id` that ties an HTTP request to every Celery task it
  triggered (propagated via `app.tasks.dispatch`).
- **Prometheus metrics** at `GET /metrics`: `sdr_email_dispatch_total`,
  `sdr_llm_tokens_total`, `sdr_llm_calls_total`, `sdr_approval_response_latency_seconds`,
  `sdr_sender_bounce_rate`, `sdr_queue_depth`.

## Security

- Admin/dashboard routes (`/dashboard`, `/approvals` list/get,
  `/leads/enrich/batch`, `/inbound/poll`, `/tracking/bounce`) require either
  an `X-API-Key` header or a JWT bearer token (`POST /auth/token` with
  `ADMIN_USERNAME`/`ADMIN_PASSWORD`).
- The approval **decision** endpoints (`/approvals/{id}/decision|approve|reject`)
  are deliberately *not* behind admin auth - they're the one-click links sent
  to Telegram/Discord/the dashboard, authenticated instead by the
  single-use, per-request `approval_token`.
- Rate limiting (slowapi) on enrichment, sending, webhooks, and approval
  decisions.
- If `ADMIN_API_KEY`/`ADMIN_USERNAME`+`ADMIN_PASSWORD` aren't set, admin
  routes are left open for local development - the app logs a loud warning
  at startup so this can't silently ship unauthenticated.

## Requirements

- Docker + Docker Compose
- An Anthropic or OpenAI API key
- SMTP credentials for sending, and (optionally) IMAP credentials for reply
  polling
- At least one of: a Telegram bot, a Discord webhook, or a generic webhook
  endpoint (or just use the built-in dashboard at `/dashboard`)

## Local setup

```bash
cp .env.example .env
# edit .env: LLM_PROVIDER + API key, SMTP_*, IMAP_*, ADMIN_API_KEY (or
# ADMIN_USERNAME/ADMIN_PASSWORD), and at least one notification channel.
# Set API_BASE_URL to a URL reachable by your notification channel (e.g. an
# ngrok URL if using Telegram/Discord from a local machine).

docker compose up --build
```

This brings up Postgres, Redis, a one-shot `migrate` service (runs
`alembic upgrade head`), the FastAPI app, a Celery worker, and Celery beat.
The API is at `http://localhost:8000` (docs at `/docs`), and the dashboard
at `http://localhost:8000/dashboard` (pass `?token=<jwt>` or an `X-API-Key`
header if auth is configured).

### Running without Docker

Requires a local Postgres and Redis.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export DATABASE_URL="postgresql+asyncpg://sdr:sdr@localhost:5432/sdr"
alembic upgrade head

uvicorn app.main:app --reload &
celery -A app.celery_app worker --loglevel=info &
celery -A app.celery_app beat --loglevel=info &
```

### Database migrations

Schema changes go through Alembic - `alembic/versions/0001_initial.py` is
the baseline. To add a migration after changing `app/models.py`:

```bash
alembic revision --autogenerate -m "describe the change"
alembic upgrade head
```

## Configuring Telegram approval buttons

1. Create a bot via [@BotFather](https://t.me/BotFather), set `TELEGRAM_BOT_TOKEN`.
2. Message the bot once, then get your chat ID (e.g. via
   `https://api.telegram.org/bot<token>/getUpdates`) and set `TELEGRAM_CHAT_ID`.
3. Point the bot's webhook at this service so button clicks come back to you:
   ```bash
   curl "https://api.telegram.org/bot<token>/setWebhook?url=${API_BASE_URL}/telegram/webhook"
   ```

## Configuring Discord

Create an Incoming Webhook in your Discord channel settings and set
`DISCORD_WEBHOOK_URL`. Discord messages include plain Approve/Reject links
(Discord webhooks can't render native interactive buttons without a bot with
slash-command/interaction support) that hit the same token-protected API.

## Multi-account sender rotation

`SMTP_*` in `.env` seeds a `default` sender account on first startup. To add
more accounts to the rotation pool (raw SMTP or an Instantly/Smartlead API
key), set `SENDER_ACCOUNTS_JSON`:

```bash
SENDER_ACCOUNTS_JSON='[{"name":"acct2","provider":"smtp","smtp_host":"smtp.example.com","smtp_port":587,"smtp_username":"u","smtp_password":"p","from_email":"u@example.com","from_name":"Sales","daily_limit":150},{"name":"instantly1","provider":"instantly","api_key":"...","from_email":"reply@example.com","daily_limit":200}]'
```

## Example: uploading leads

CSV (`leads.csv`):

```csv
company_name,contact_name,website,email,linkedin_url
Acme Corp,Jane Doe,https://acme.com,jane@acme.com,https://linkedin.com/in/janedoe
```

```bash
curl -F "file=@leads.csv" http://localhost:8000/leads/upload/file
```

Or JSON:

```bash
curl -X POST http://localhost:8000/leads/upload \
  -H "Content-Type: application/json" \
  -d '{"leads":[{"company_name":"Acme Corp","contact_name":"Jane Doe","email":"jane@acme.com"}]}'
```

## Testing

```bash
pip install -r requirements.txt
pytest
```

The suite runs against SQLite (both the async and sync engines point at the
same file, so router-created data is visible to service-layer code under
test) - no Postgres/Redis needed. Covers: LLM structured-output
self-correction and give-up-after-N-attempts behavior, the agentic
draft/critique/revise loop, IMAP body/header parsing edge cases (multipart,
attachments, RFC 2047 encoded headers, non-UTF8 charsets), approval-gate
race conditions (concurrent approve/reject, wrong token, double-resolve),
circuit breaker state transitions, and sender-account bounce-rate auto-pause.

## Error handling notes

- All LLM calls request strict JSON, validate it against a Pydantic schema,
  and retry with the exact validation error fed back to the model
  (self-correction) up to 3 attempts before surfacing a clear error.
- SMTP/IMAP/provider-API calls retry transient failures and 429s with
  exponential backoff + jitter, behind a circuit breaker per account/provider.
- A sentiment classification failure never defaults to "positive" - it's
  left unset for manual/automated retry rather than risking a false
  approval-gate trigger.
- Notification fan-out is best-effort per channel; the approval request is
  still created (and visible on `/dashboard`) even if every notification
  channel fails to deliver.
- If every sender account is paused/over its limit/circuit-open, sends fail
  with a clear `NoHealthySenderError` rather than silently sending from an
  unhealthy account.

## Security notes

- `.env` is git-ignored; only `.env.example` (with no real secrets) is
  committed.
- Approval decisions require a per-request, single-use random token
  (`approval_token`) bound to that `ApprovalRequest` - links/buttons cannot
  be guessed or replayed against a different request, and the PENDING ->
  resolved transition is race-safe (see Architecture above).
