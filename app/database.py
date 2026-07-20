"""Async SQLAlchemy engine/session setup for the FastAPI app.

Celery workers (and Alembic) use the synchronous engine in app/db_sync.py
instead, since they don't run inside an asyncio event loop.
"""
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

settings = get_settings()

_engine_kwargs = {"pool_pre_ping": True}
if not settings.database_url.startswith("sqlite"):
    # Pool sizing is meaningless for SQLite's single-file NullPool and
    # raises a TypeError if passed - only applies to Postgres in production.
    _engine_kwargs.update(pool_size=10, max_overflow=20)

engine = create_async_engine(settings.database_url, **_engine_kwargs)
AsyncSessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


async def init_db():
    """Create tables if they don't exist yet. Alembic migrations are the
    source of truth in production; this is a convenience for local/dev use
    when migrations haven't been run."""
    from app import models  # noqa: F401 ensure models are registered

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
