"""
Structured JSON logging via structlog, wired to also capture every plain
`logging.getLogger(__name__)` call used throughout app.services/app.routers
(via structlog.stdlib.ProcessorFormatter) so ALL logs - not just
structlog-native ones - get the same JSON shape and the same request/
correlation ID.

The request ID is a ContextVar (request_id_var): app.main's HTTP middleware
sets it per-request, and app.tasks.dispatch.with_request_context sets it per
Celery task run (propagated from the enqueueing request, or freshly
generated for beat-triggered tasks) - so one ID ties an API call to every
background task it caused.
"""
import logging
import sys
import uuid
from contextvars import ContextVar

import structlog

from app.config import get_settings

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_configured = False


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def _add_request_id(logger, method_name, event_dict):
    event_dict["request_id"] = request_id_var.get()
    return event_dict


def configure_logging() -> None:
    global _configured
    if _configured:
        return
    _configured = True

    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        _add_request_id,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=shared_processors + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    final_renderer = structlog.processors.JSONRenderer() if settings.log_json else structlog.dev.ConsoleRenderer()
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, final_renderer],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(level)


def get_logger(name: str):
    return structlog.get_logger(name)
