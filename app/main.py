"""FastAPI application entrypoint for the AI SDR backend."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.database import init_db
from app.routers import approvals, dashboard, inbound, leads, outbound, telegram, tracking
from app.tasks.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_scheduler()
    logger.info("AI SDR backend started (env=%s)", get_settings().app_env)
    yield
    stop_scheduler()


app = FastAPI(
    title="AI SDR Backend",
    description=(
        "Modular AI Sales Development Representative backend: lead ingestion & "
        "enrichment, outbound sending & tracking, reply sentiment triage, and a "
        "strict human-in-the-loop approval gate for any AI-drafted reply."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(leads.router)
app.include_router(outbound.router)
app.include_router(tracking.router)
app.include_router(inbound.router)
app.include_router(approvals.router)
app.include_router(telegram.router)
app.include_router(dashboard.router)


@app.get("/health")
def health():
    return {"status": "ok"}
