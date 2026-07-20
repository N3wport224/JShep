# AI SDR Backend

A containerized, modular AI Sales Development Representative (SDR) backend. It
automates outbound prospecting and reply sentiment triage, but **never** sends
an AI-drafted reply without explicit human approval.

## Architecture

```
app/
  main.py                FastAPI app assembly, startup/shutdown, scheduler
  config.py               Settings (env vars)
  database.py              SQLAlchemy engine/session
  models.py                 Lead, EmailMessage, Reply, ApprovalRequest
  schemas.py                 Pydantic request/response models
  routers/
    leads.py                  CSV/JSON ingestion + LLM enrichment
    outbound.py                 Send drafted emails, list sent messages
    tracking.py                   Open-tracking pixel endpoint
    inbound.py                     IMAP poll trigger + generic reply webhook
    approvals.py                     The human-in-the-loop approval gate API
    telegram.py                       Telegram inline-button callback receiver
    dashboard.py                       Minimal local HTML approval dashboard
  services/
    llm.py                    Anthropic/OpenAI wrapper (copy, sentiment, drafts)
    email_sender.py             SMTP sending + open-tracking pixel injection
    imap_listener.py              IMAP polling + reply matching
    sentiment.py                    Sentiment triage -> approval gate
    notifier.py                      Telegram / Discord / generic webhook fan-out
  tasks/
    scheduler.py               APScheduler: IMAP polling + follow-up sequence
```

### Flow

1. **Ingest** leads via CSV or JSON (`POST /leads/upload`).
2. **Enrich** a lead into a personalized cold email draft via the LLM
   (`POST /leads/{id}/enrich`).
3. **Send** the draft (`POST /outbound/send/{message_id}`). The scheduler then
   automatically sends follow-up steps (configurable day offsets) to any lead
   that hasn't replied, and tracks opens via a 1x1 pixel.
4. **Replies** are picked up either by the IMAP poller (runs on a schedule and
   can be triggered manually via `POST /inbound/poll`) or pushed in from an
   outreach platform via `POST /inbound/webhook`.
5. Each reply is classified by the LLM into exactly one of:
   `positive_interested`, `objection`, `negative_opt_out`.
6. **`positive_interested` replies never auto-reply.** The system generates a
   draft response, creates a `PENDING` `ApprovalRequest`, and pushes an
   interactive notification (Telegram inline buttons, and/or a Discord/generic
   webhook message with Approve/Reject links) containing the prospect's
   message and the AI draft.
7. Execution stops there. The reply is only ever sent after a human calls
   `GET/POST /approvals/{id}/decision` (or clicks a button/link) with a valid,
   single-use token. Rejecting simply marks the request `REJECTED` so a human
   can take over manually — nothing is sent.

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
# edit .env: fill in LLM_PROVIDER + API key, SMTP_*, IMAP_*, and at least one
# notification channel (TELEGRAM_*, DISCORD_WEBHOOK_URL, or
# GENERIC_NOTIFY_WEBHOOK_URL). Set API_BASE_URL to a URL reachable by your
# notification channel (e.g. an ngrok URL if using Telegram/Discord from a
# local machine).

docker compose up --build
```

The API is now available at `http://localhost:8000` (interactive docs at
`/docs`), and the human-in-the-loop dashboard at `http://localhost:8000/dashboard`.

### Running without Docker

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
mkdir -p data
export DATABASE_URL="sqlite:///./data/sdr.db"
uvicorn app.main:app --reload
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

## Error handling notes

- All LLM calls request strict JSON and retry (with exponential backoff) on
  rate limits and on malformed JSON responses, up to a bounded number of
  attempts, before surfacing a clear error.
- SMTP sends retry transient failures with exponential backoff.
- A sentiment classification failure never defaults to "positive" — it's left
  unset for manual/automated retry rather than risking a false approval-gate
  trigger.
- Notification fan-out is best-effort per channel; the approval request is
  still created (and visible on `/dashboard`) even if every notification
  channel fails to deliver.

## Security notes

- Approval decisions require a per-request, single-use random token
  (`approval_token`) bound to that `ApprovalRequest` — links/buttons cannot be
  guessed or replayed against a different request.
- `.env` is git-ignored; only `.env.example` (with no real secrets) is
  committed.
