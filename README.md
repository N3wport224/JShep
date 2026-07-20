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
    suppression.py                       Suppression list admin API + public unsubscribe
    campaigns.py                         A/B campaign CRUD + per-variant stats
  services/
    llm.py                    Anthropic/OpenAI wrapper: structured output validation
                                with self-correction retries, agentic reply loop,
                                meeting-intent detection
    email_sender.py             Low-level SMTP dispatch (used by sender_rotation)
    sender_rotation.py            Multi-account sender pool, provider abstraction
                                    (SMTP/Instantly/Smartlead), bounce/health tracking
    imap_listener.py                IMAP polling + reply matching + body parsing
    sentiment.py                      Sentiment triage -> agentic draft -> approval gate;
                                        also triggers CRM sync + suppression on the way
    memory.py                          Per-lead conversation memory (ThreadMessage)
    notifier.py                         Telegram / Discord / generic webhook fan-out
    suppression.py                       Global do-not-contact list + unsubscribe footer
    crm.py                                 CRM contact push (HubSpot)
    calendar.py                              Booking-link lookup for meeting requests
    campaigns.py                               A/B variant assignment + metric tracking
  tasks/
    celery_tasks.py            All background task bodies (enrich, send, poll, triage,
                                 follow-up sequence, bounce health check)
    dispatch.py                  enqueue() / request-ID propagation into tasks
alembic/                 Database migrations (source of truth for schema)
tests/                    pytest suite (LLM self-correction, IMAP parsing quirks,
                            approval race conditions, circuit breaker, bounce auto-pause,
                            suppression enforcement, CRM sync, A/B assignment/metrics)
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
6. **`positive_interested` replies never auto-reply.** The system checks
   whether the prospect asked to schedule a call (`detect_meeting_intent`),
   pushes the lead to the configured CRM (`app.services.crm`), then runs a
   multi-step agentic loop (draft -> self-critique -> revise, using the
   lead's *entire* conversation thread as context, and a calendar booking
   link if a meeting was requested) and creates a `PENDING`
   `ApprovalRequest`, then pushes an interactive notification (Telegram
   inline buttons, and/or a Discord/generic webhook message with
   Approve/Reject links) containing the prospect's message and the AI draft.
7. A human resolves the decision via `GET/POST /approvals/{id}/decision`
   (or the Telegram/Discord button/link). This transition is a single
   **atomic conditional UPDATE** (`WHERE status = 'pending'`), so two
   concurrent clicks can't both succeed - the loser gets `409`. Only after
   that commits does `send_approved_reply_task` get queued to actually send
   (still gated by the suppression check below).
8. A **`negative_opt_out`** reply immediately adds the lead's email *and
   domain* to the global suppression list - see Compliance below.

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
  `sdr_sender_bounce_rate`, `sdr_queue_depth`, `sdr_crm_sync_total`,
  `sdr_suppression_total`, `sdr_variant_sent_total`, `sdr_variant_open_total`,
  `sdr_variant_reply_total`, `sdr_variant_positive_total`.

## CRM & calendar integration

- Every `positive_interested` reply is pushed to the configured CRM
  (`CRM_PROVIDER=hubspot` + `HUBSPOT_ACCESS_TOKEN`) as a contact via
  `app.services.crm.sync_lead_to_crm`, recording `Lead.crm_contact_id` /
  `crm_synced_at`. A HubSpot 409 (contact already exists) is treated as
  success and reuses the existing contact ID. A CRM outage is logged and
  counted (`sdr_crm_sync_total{status="failed"}`) but never blocks triage or
  the approval gate - CRM sync is additive record-keeping, not on the
  send-a-reply critical path.
- If a reply asks to schedule a call, `LLMClient.detect_meeting_intent`
  flags it and the agentic drafting loop is handed `CALENDAR_BOOKING_URL`
  (a Cal.com event link or Google Calendar appointment schedule link) to
  include in its draft - the booking flow itself (availability, timezones,
  confirmation) is handled entirely by Cal.com/Google Calendar, not by this
  service.
- To add another CRM, implement `app.services.crm.CRMProvider` and register
  it in `get_crm_provider()`.

## Compliance: global suppression list (CAN-SPAM / GDPR)

- `SuppressionEntry` (email + domain, unique) is the global do-not-contact
  list. **Every** outbound send path - `enrich_lead_task`, `send_email_task`,
  `follow_up_sequence_task`, and `send_approved_reply_task` - checks
  `app.services.suppression.is_suppressed` before doing anything, and
  suppression matches on **domain**, not just the exact address, so once one
  address at a domain hard-opts-out, nothing at that domain can be targeted
  again by accident (a stale queued task, a re-uploaded CSV, a new campaign).
- A `negative_opt_out` sentiment classification suppresses automatically.
  So does clicking the unsubscribe link that's appended as a footer to every
  cold email and follow-up (`GET /unsubscribe/{lead_id}?token=...` - a
  public, single-use-token-protected link, not admin-gated, so recipients
  can always opt out without authenticating).
- Admins can also manage the list directly: `GET/POST /suppression`,
  `DELETE /suppression/{id}`.

## Campaign A/B testing

- `POST /campaigns` creates a campaign with 2+ variants, each with an
  optional `prompt_hint` (e.g. "punchy question hook" vs. "direct value
  statement") that's injected into the cold-email generation prompt so
  variants are actually distinguishable, not converging on the same copy.
- Enroll leads by passing `campaign_id` to `POST /leads/upload` (JSON body)
  or `POST /leads/upload/file?campaign_id=...` (CSV/JSON file). Each lead is
  deterministically assigned one variant (hash of lead ID, weighted by
  `CampaignVariant.weight`) at first enrichment and keeps it for its whole
  lifecycle, including follow-ups, so metrics stay statistically clean.
- Every send, open, reply, and positive-sentiment event increments that
  lead's variant counters (`sent_count`/`open_count`/`reply_count`/
  `positive_count`) *and* the matching Prometheus counters
  (`sdr_variant_sent_total`, `sdr_variant_open_total`,
  `sdr_variant_reply_total`, `sdr_variant_positive_total`, both labeled
  `campaign`+`variant`). `GET /campaigns/{id}/stats` returns computed
  open/reply/positive rates per variant from the same source-of-truth counters.

## Security

- Admin/dashboard routes (`/dashboard`, `/approvals` list/get,
  `/leads/enrich/batch`, `/inbound/poll`, `/tracking/bounce`, `/suppression`,
  `/campaigns`) require either an `X-API-Key` header or a JWT bearer token
  (`POST /auth/token` with `ADMIN_USERNAME`/`ADMIN_PASSWORD`).
- The approval **decision** endpoints (`/approvals/{id}/decision|approve|reject`)
  and `GET /unsubscribe/{lead_id}` are deliberately *not* behind admin auth -
  they're one-click links sent to a human outside this system (Telegram/
  Discord/email), authenticated instead by their own single-use,
  per-request token (`ApprovalRequest.approval_token` / `Lead.unsubscribe_token`).
- Rate limiting (slowapi) on enrichment, sending, webhooks, approval
  decisions, and unsubscribe requests.
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

## Example: A/B testing a campaign

```bash
curl -X POST http://localhost:8000/campaigns \
  -H "X-API-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"name":"spring-outreach","variants":[
        {"label":"A","prompt_hint":"open with a punchy question hook","weight":1},
        {"label":"B","prompt_hint":"lead with a direct value statement","weight":1}
      ]}'
# -> {"id": "<campaign_id>", ...}

curl -X POST http://localhost:8000/leads/upload \
  -H "Content-Type: application/json" \
  -d '{"campaign_id":"<campaign_id>","leads":[{"company_name":"Acme Corp","contact_name":"Jane Doe","email":"jane@acme.com"}]}'

curl http://localhost:8000/campaigns/<campaign_id>/stats -H "X-API-Key: $ADMIN_API_KEY"
```

## Example: managing the suppression list

```bash
# Manually block an address (e.g. a compliance/legal request)
curl -X POST http://localhost:8000/suppression \
  -H "X-API-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"email":"do-not-contact@example.com","reason":"legal request"}'

curl http://localhost:8000/suppression -H "X-API-Key: $ADMIN_API_KEY"
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
draft/critique/revise loop and meeting-intent detection, IMAP body/header
parsing edge cases (multipart, attachments, RFC 2047 encoded headers,
non-UTF8 charsets), approval-gate race conditions (concurrent approve/reject,
wrong token, double-resolve), circuit breaker state transitions,
sender-account bounce-rate auto-pause, suppression-list enforcement across
every send path, CRM sync success/failure/no-op handling, and A/B variant
assignment determinism + metric tracking.

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
