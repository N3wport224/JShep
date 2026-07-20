"""
Helpers for enqueuing Celery tasks from the (async) FastAPI web layer with
the current request's correlation ID attached, and for Celery tasks to pick
that ID back up so structured logs tie a background job back to the HTTP
request that triggered it.
"""
import functools

from app.core.logging import get_logger, new_request_id, request_id_var

logger = get_logger(__name__)


def enqueue(task, *args, **kwargs) -> str:
    """Call task.delay(*args, request_id=..., **kwargs) with the current
    request ID (or a freshly generated one if there isn't one - e.g. Celery
    beat triggering a task with no originating HTTP request)."""
    request_id = request_id_var.get()
    if request_id == "-":
        request_id = new_request_id()
    async_result = task.apply_async(args=args, kwargs={**kwargs, "request_id": request_id})
    return async_result.id


def with_request_context(fn):
    """Decorator for Celery task bodies: pops `request_id` out of kwargs,
    binds it to the logging contextvar for the task's duration, and restores
    the previous value afterwards."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        request_id = kwargs.pop("request_id", None) or new_request_id()
        token = request_id_var.set(request_id)
        try:
            return fn(*args, **kwargs)
        except Exception:
            logger.error("task_failed", task=fn.__name__, exc_info=True)
            raise
        finally:
            request_id_var.reset(token)

    return wrapper
