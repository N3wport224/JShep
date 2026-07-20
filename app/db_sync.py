"""Synchronous SQLAlchemy engine/session, used by Celery workers and Alembic
migrations - neither runs inside an asyncio event loop, so the async engine
in app/database.py isn't usable there. Shares the same declarative models
and Base metadata as the async engine.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.database import Base

settings = get_settings()

_engine_kwargs = {"pool_pre_ping": True}
if settings.sync_database_url.startswith("sqlite"):
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    # Pool sizing is meaningless for SQLite's single-file NullPool and
    # raises a TypeError if passed - only applies to Postgres in production.
    _engine_kwargs.update(pool_size=5, max_overflow=10)

sync_engine = create_engine(settings.sync_database_url, **_engine_kwargs)
SessionLocalSync = sessionmaker(autocommit=False, autoflush=False, bind=sync_engine)


def get_db_sync():
    db = SessionLocalSync()
    try:
        yield db
    finally:
        db.close()
