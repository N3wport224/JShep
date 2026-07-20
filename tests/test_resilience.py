"""Circuit breaker state machine used by every outbound integration."""
import time

import pytest

from app.core.resilience import CircuitBreaker, CircuitBreakerOpenError, CircuitState


def test_breaker_starts_closed():
    breaker = CircuitBreaker(name="test", failure_threshold=3, recovery_timeout_seconds=1)
    assert breaker.state == CircuitState.CLOSED
    breaker.before_call()  # should not raise


def test_breaker_opens_after_failure_threshold():
    breaker = CircuitBreaker(name="test", failure_threshold=3, recovery_timeout_seconds=60)
    for _ in range(3):
        breaker.on_failure()
    assert breaker.state == CircuitState.OPEN
    with pytest.raises(CircuitBreakerOpenError):
        breaker.before_call()


def test_breaker_success_resets_failure_count():
    breaker = CircuitBreaker(name="test", failure_threshold=3, recovery_timeout_seconds=60)
    breaker.on_failure()
    breaker.on_failure()
    breaker.on_success()
    breaker.on_failure()
    breaker.on_failure()
    # only 2 consecutive failures since the reset - should still be closed
    assert breaker.state == CircuitState.CLOSED


def test_breaker_half_opens_after_recovery_timeout():
    breaker = CircuitBreaker(name="test", failure_threshold=1, recovery_timeout_seconds=0.05)
    breaker.on_failure()
    assert breaker.state == CircuitState.OPEN
    time.sleep(0.1)
    assert breaker.state == CircuitState.HALF_OPEN
    breaker.before_call()  # half-open allows a trial call through


def test_breaker_half_open_failure_reopens_immediately():
    breaker = CircuitBreaker(name="test", failure_threshold=5, recovery_timeout_seconds=0.05)
    breaker.on_failure()  # below threshold, stays closed
    assert breaker.state == CircuitState.CLOSED
    breaker.on_failure()
    breaker.on_failure()
    breaker.on_failure()
    breaker.on_failure()
    assert breaker.state == CircuitState.OPEN
    time.sleep(0.1)
    assert breaker.state == CircuitState.HALF_OPEN
    breaker.on_failure()  # a single half-open failure reopens, doesn't need full threshold
    assert breaker.state == CircuitState.OPEN


def test_breaker_call_wraps_success_and_failure():
    breaker = CircuitBreaker(name="test", failure_threshold=2, recovery_timeout_seconds=60)

    assert breaker.call(lambda x: x + 1, 1) == 2

    def boom():
        raise ValueError("nope")

    with pytest.raises(ValueError):
        breaker.call(boom)
    with pytest.raises(ValueError):
        breaker.call(boom)
    assert breaker.state == CircuitState.OPEN
    with pytest.raises(CircuitBreakerOpenError):
        breaker.call(lambda: "unreachable")
