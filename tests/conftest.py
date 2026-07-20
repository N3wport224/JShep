"""
Test configuration. Points both the async (FastAPI) and sync (Celery/
services) database engines at the same SQLite file so a Lead created via
the async test client is visible to code exercised through the sync
session, without needing a real Postgres instance for the test suite.

Env vars are set *before* importing anything under app/, since
app.database / app.db_sync build their engines at import time from cached
settings.
"""
import asyncio
import os
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

TEST_DB_PATH = Path(__file__).parent / "test_sdr.db"

os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{TEST_DB_PATH}")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("ADMIN_API_KEY", "test-admin-key")

from app.database import Base, engine  # noqa: E402
from app.db_sync import sync_engine  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _clean_test_db():
    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink()
    yield
    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink()


@pytest_asyncio.fixture(autouse=True)
async def _create_schema(_clean_test_db):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield


@pytest.fixture
def sync_db():
    from app.db_sync import SessionLocalSync

    db = SessionLocalSync()
    try:
        yield db
    finally:
        db.close()


@pytest_asyncio.fixture
async def async_db():
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        yield session


@pytest_asyncio.fixture
async def client():
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def make_lead_kwargs(**overrides) -> dict:
    defaults = {
        "company_name": "Acme Corp",
        "contact_name": "Jane Doe",
        "website": "https://acme.example.com",
        "email": f"jane-{uuid.uuid4().hex[:8]}@acme.example.com",
        "linkedin_url": None,
    }
    defaults.update(overrides)
    return defaults
