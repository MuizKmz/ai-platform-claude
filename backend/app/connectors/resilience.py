"""Circuit breaker and retry, shared by any connector that calls out.

A connector talks to a system nobody here operates. It will be down, slow, or
rate-limited at some point, and the failure mode that matters is not the outage
itself — it is what our platform does during one.

**Retrying makes an outage worse.** A struggling upstream that is timing out
receives three times the load from a client that retries, and the retries arrive
exactly when it can least absorb them. So retry only on errors that are plausibly
transient, with backoff, and few times.

**A circuit breaker is what stops that entirely.** After enough consecutive
failures it stops calling for a while, which lets the upstream recover and
returns an immediate error to the caller instead of making them wait for a
timeout that is already known to be coming.

State is per-process and in memory. That is deliberate: a shared breaker in Redis
would add a network call to the path taken when the network is already the
problem. The cost is that N processes each learn the upstream is down separately,
which is acceptable — each still stops hammering it.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum


class CircuitOpenError(Exception):
    """The breaker is open; the call was not attempted.

    Distinct from an upstream failure on purpose. "We did not try" and "we tried
    and it failed" are different facts, and a trace that conflates them cannot
    show how long an outage actually lasted.
    """


class CircuitState(StrEnum):
    CLOSED = "closed"  # normal: calls pass through
    OPEN = "open"  # failing: calls are refused without being attempted
    HALF_OPEN = "half_open"  # probing: one call allowed to test recovery


@dataclass
class CircuitBreaker:
    """Per-connector failure tracking."""

    failure_threshold: int = 5
    # How long to stay open before probing. Long enough for a restart, short
    # enough that a recovered upstream is noticed quickly.
    recovery_seconds: float = 30.0

    _failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)
    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)

    @property
    def state(self) -> CircuitState:
        """Current state, moving OPEN to HALF_OPEN once the timer has elapsed."""
        if (
            self._state is CircuitState.OPEN
            and self._opened_at is not None
            and time.monotonic() - self._opened_at >= self.recovery_seconds
        ):
            self._state = CircuitState.HALF_OPEN
        return self._state

    def before_call(self) -> None:
        """Raise CircuitOpenError if calls are currently refused."""
        if self.state is CircuitState.OPEN:
            raise CircuitOpenError(
                "The upstream system is unavailable and calls are paused. "
                "It will be retried shortly."
            )

    def record_success(self) -> None:
        """Reset. One success closes the circuit, including from HALF_OPEN."""
        self._failures = 0
        self._opened_at = None
        self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        """Count a failure and open the circuit if the threshold is reached.

        A failure while HALF_OPEN re-opens immediately rather than counting
        toward the threshold again: the probe was the test, and it failed.
        """
        if self._state is CircuitState.HALF_OPEN:
            self._trip()
            return

        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._trip()

    def _trip(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = time.monotonic()
        self._failures = self.failure_threshold


@dataclass(frozen=True)
class RetryPolicy:
    """How many times, and how long between."""

    # Total attempts including the first. Three is a common ceiling: beyond it
    # the caller has usually given up anyway, and each attempt adds load to a
    # system that is already struggling.
    attempts: int = 3
    base_delay: float = 0.25
    max_delay: float = 4.0
    # Randomises the delay so a hundred clients failing together do not all
    # retry at the same instant and re-create the spike that caused the failure.
    jitter: bool = True

    def delay_for(self, attempt: int) -> float:
        """Exponential backoff for a 1-based attempt number."""
        delay: float = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
        if self.jitter:
            # Full jitter: uniform in [0, delay]. Spreads retries more evenly
            # than adding a small random offset to a fixed backoff.
            delay = random.uniform(0, delay)  # noqa: S311 — jitter, not crypto
        return delay


def call_with_resilience[T](
    operation: Callable[[], T],
    *,
    breaker: CircuitBreaker,
    retry: RetryPolicy,
    is_retryable: Callable[[Exception], bool],
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run `operation`, retrying transient failures, honouring the breaker.

    `is_retryable` is supplied by the caller because only it knows its own
    errors. Retrying a 404 or a 401 is pointless and wastes an upstream's
    capacity on a request that will fail identically every time.
    """
    breaker.before_call()

    last_error: Exception | None = None
    for attempt in range(1, retry.attempts + 1):
        try:
            result = operation()
        except Exception as exc:  # noqa: BLE001 — classification is the caller's job
            last_error = exc
            if not is_retryable(exc):
                # A permanent failure still counts toward the breaker: a stream
                # of 500s is an outage even though retrying each one is futile.
                breaker.record_failure()
                raise
            if attempt == retry.attempts:
                breaker.record_failure()
                raise
            sleep(retry.delay_for(attempt))
        else:
            breaker.record_success()
            return result

    # Unreachable: the loop either returns or raises.
    raise last_error if last_error else RuntimeError("retry loop exited unexpectedly")
