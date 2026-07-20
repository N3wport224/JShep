"""FastAPI application entrypoint for the AI SDR backend.

The web process is intentionally thin: async CRUD + Celery task enqueueing
only. All LLM/SMTP/IMAP work happens in Celery workers (see
app.tasks.celery_tasks) driven by Celery beat (see app.celery_app) - see the
README's "Architecture" section for the full request/task flow.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import Response
from prometheus_client import generate_latest
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.config import get_settings
from app.core.logging import configure_logging, get_logger, new_request_id, request_id_var
from app.core.security import auth_is_configured, limiter
from app.database import init_db
from app.db_sync import SessionLocalSync
from app.routers import (
    approvals,
    auth,
    campaigns,
    dashboard,
    inbound,
    leads,
    outbound,
    senders,
    sequences,
    suppression,
    telegram,
    tracking,
)
from app.services.sender_rotation import seed_sender_accounts_from_env

configure_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    await init_db()

    # Sender accounts + Celery beat schedule both live outside the request
    # path, but seeding uses the sync engine so it can share app.db_sync
    # with the workers.
    db = SessionLocalSync()
    try:
        seed_sender_accounts_from_env(db)
    finally:
        db.close()

    if not auth_is_configured():
        logger.warning(
            "auth_not_configured",
            message=(
                "ADMIN_API_KEY / ADMIN_USERNAME+ADMIN_PASSWORD are not set - "
                "admin and dashboard routes are UNAUTHENTICATED. Set these before "
                "deploying anywhere reachable outside localhost."
            ),
        )

    logger.info("app_started", env=settings.app_env)
    yield


app = FastAPI(
    title="AI SDR Backend",
    description=(
        "Modular AI Sales Development Representative backend: lead ingestion & "
        "enrichment, outbound sending & tracking, reply sentiment triage, and a "
        "strict human-in-the-loop approval gate for any AI-drafted reply. "
        "Postgres + Celery/Redis backed, with resilience, agentic reply drafting, "
        "full observability, CRM/calendar integration, a global compliance "
        "suppression list, campaign A/B testing, sender health/warmup guardian, "
        "multi-channel (email + LinkedIn) sequencing, and a real-time funnel dashboard."
    ),
    version="4.0.0",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    incoming = request.headers.get("X-Request-ID")
    token = request_id_var.set(incoming or new_request_id())
    try:
        response = await call_next(request)
    finally:
        request_id_var.reset(token)
    response.headers["X-Request-ID"] = request_id_var.get()
    return response


app.include_router(auth.router)
app.include_router(leads.router)
app.include_router(outbound.router)
app.include_router(tracking.router)
app.include_router(inbound.router)
app.include_router(approvals.router)
app.include_router(telegram.router)
app.include_router(dashboard.router)
app.include_router(suppression.router)
app.include_router(campaigns.router)
app.include_router(senders.router)
app.include_router(sequences.router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    return Response(content=generate_latest(), media_type="text/plain; version=0.0.4; charset=utf-8")
