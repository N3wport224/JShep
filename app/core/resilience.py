"""
Resilience primitives shared by every outbound integration (SMTP, IMAP,
sender-provider APIs, LLM calls): a simple circuit breaker plus a
tenacity retry preset with exponential backoff and jitter.

The circuit breaker exists on top of retries because retries alone keep
hammering a genuinely-down provider; once a breaker trips, calls fail fast
(no network round trip) until the recovery timeout elapses, which is what
protects queue throughput when e.g. an SMTP host or the LLM provider is
having a real outage rather than a transient blip.
"""
import enum
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, TypeVar

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitState(str, enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerOpenError(Exception):
    """Raised when a call is rejected because the circuit is open."""


@dataclass
class CircuitBreaker:
    name: str
    failure_threshold: int = 5
    recovery_timeout_seconds: float = 30.0
    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _failure_count: int = field(default=0, init=False)
    _opened_at: float = field(default=0.0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    @property
    def state(self) -> CircuitState:
        with self._lock:
            if self._state == CircuitState.OPEN and self._should_attempt_reset():
                self._state = CircuitState.HALF_OPEN
            return self._state

    def _should_attempt_reset(self) -> bool:
        return (time.monotonic() - self._opened_at) >= self.recovery_timeout_seconds

    def before_call(self) -> None:
        if self.state == CircuitState.OPEN:
            raise CircuitBreakerOpenError(
                f"Circuit '{self.name}' is open - failing fast until it recovers "
                f"(retry after ~{self.recovery_timeout_seconds:.0f}s)"
            )

    def on_success(self) -> None:
        with self._lock:
            self._failure_count = 0
            if self._state != CircuitState.CLOSED:
                logger.info("Circuit '%s' closing after successful call", self.name)
            self._state = CircuitState.CLOSED

    def on_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            if self._state == CircuitState.HALF_OPEN or self._failure_count >= self.failure_threshold:
                if self._state != CircuitState.OPEN:
                    logger.warning(
                        "Circuit '%s' opening after %d consecutive failures", self.name, self._failure_count
                    )
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()

    def call(self, fn: Callable[..., T], *args, **kwargs) -> T:
        self.before_call()
        try:
            result = fn(*args, **kwargs)
        except Exception:
            self.on_failure()
            raise
        else:
            self.on_success()
            return result


_breakers: dict[str, CircuitBreaker] = {}
_breakers_lock = threading.Lock()


def get_circuit_breaker(
    name: str, failure_threshold: int = 5, recovery_timeout_seconds: float = 30.0
) -> CircuitBreaker:
    with _breakers_lock:
        if name not in _breakers:
            _breakers[name] = CircuitBreaker(
                name=name, failure_threshold=failure_threshold, recovery_timeout_seconds=recovery_timeout_seconds
            )
        return _breakers[name]


def all_circuit_states() -> dict[str, str]:
    with _breakers_lock:
        return {name: breaker.state.value for name, breaker in _breakers.items()}


class RateLimitedError(Exception):
    """Raised by provider wrappers on HTTP 429 / explicit rate-limit signals
    so the retry policy below can back off specifically for those."""


def resilient_retry(retryable_exceptions: tuple[type[Exception], ...] = (RateLimitedError, ConnectionError, TimeoutError)):
    """Tenacity decorator: exponential backoff with jitter, bounded attempts.
    Use on any outbound call (SMTP, IMAP, provider API, LLM) that can hit
    transient failures or rate limits."""
    return retry(
        reraise=True,
        stop=stop_after_attempt(5),
        wait=wait_exponential_jitter(initial=1, max=60, jitter=2),
        retry=retry_if_exception_type(retryable_exceptions),
    )
