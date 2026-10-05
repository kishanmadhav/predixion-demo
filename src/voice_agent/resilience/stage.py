"""ResilientStage: one pipeline stage wrapped in timeout + circuit breaker + retry.

Composition is Retry(Breaker(Timeout(call))): every attempt asks the breaker for a
permit, so the breaker sees raw provider health, and a rejection from an open
breaker ends the stage immediately instead of being retried.

All attempts are recorded as `AttemptRecord`s. On failure they travel with the
`StageFailedError` into the dead-letter queue, so an operator can see exactly what
was tried, when, and why it failed.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any, Generic, Literal, TypeVar

from voice_agent.providers.base import ErrorKind, ProviderError
from voice_agent.resilience.breaker import CircuitBreaker
from voice_agent.resilience.retry import RetryPolicy

T = TypeVar("T")

# Never start an attempt (or sleep into one) with less budget than this left.
MIN_ATTEMPT_BUDGET_S = 0.05


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    stage: str
    attempt: int
    outcome: Literal["ok", "error", "rejected"]
    duration_ms: float
    backoff_ms: float = 0.0
    error_kind: str | None = None
    status_code: int | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class StageOutcome(Generic[T]):
    value: T
    attempts: list[AttemptRecord]


class StageFailedError(Exception):
    def __init__(self, stage: str, error: ProviderError, attempts: list[AttemptRecord]) -> None:
        super().__init__(f"stage {stage!r} failed after {len(attempts)} attempt(s): {error}")
        self.stage = stage
        self.error = error
        self.attempts = attempts


AttemptObserver = Callable[[str, AttemptRecord], None]


class ResilientStage:
    def __init__(
        self,
        name: str,
        *,
        breaker: CircuitBreaker,
        retry: RetryPolicy,
        timeout_s: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        observer: AttemptObserver | None = None,
    ) -> None:
        self.name = name
        self.breaker = breaker
        self.retry = retry
        self.timeout_s = timeout_s
        self._sleep = sleep
        self._clock = clock
        self._observer = observer

    async def run(self, fn: Callable[[], Awaitable[T]], *, deadline: float) -> StageOutcome[T]:
        """Run `fn` until it succeeds, a non-retryable error occurs, attempts run out,
        or the turn `deadline` (a `clock()` timestamp) would be exceeded."""
        attempts: list[AttemptRecord] = []
        number = 0
        while True:
            number += 1
            remaining = deadline - self._clock()
            if remaining < MIN_ATTEMPT_BUDGET_S:
                err = ProviderError(
                    self.name, ErrorKind.DEADLINE, f"turn deadline reached before attempt {number}"
                )
                raise StageFailedError(self.name, err, attempts)

            try:
                permit = self.breaker.acquire()
            except ProviderError as rejected:
                self._record(attempts, number, "rejected", 0.0, rejected)
                raise StageFailedError(self.name, rejected, attempts) from None

            started = self._clock()
            attempt_timeout = min(self.timeout_s, remaining)
            try:
                async with asyncio.timeout(attempt_timeout):
                    value = await fn()
            except TimeoutError:
                err = ProviderError(
                    self.name, ErrorKind.TIMEOUT, f"no response within {attempt_timeout:.2f}s"
                )
            except ProviderError as provider_error:
                err = provider_error
            except BaseException:
                self.breaker.release(permit)
                raise
            else:
                self.breaker.on_success(permit)
                self._record(attempts, number, "ok", self._elapsed_ms(started))
                return StageOutcome(value, attempts)

            duration_ms = self._elapsed_ms(started)
            if err.counts_as_failure:
                self.breaker.on_failure(permit)
            else:
                self.breaker.release(permit)

            delay = self._retry_delay(number, err, deadline)
            if delay is None:
                self._record(attempts, number, "error", duration_ms, err)
                raise StageFailedError(self.name, err, attempts) from err

            self._record(attempts, number, "error", duration_ms, err, backoff_s=delay)
            await self._sleep(delay)

    def _retry_delay(self, number: int, err: ProviderError, deadline: float) -> float | None:
        """Seconds to back off before the next attempt, or None to give up."""
        if not self.retry.should_retry(attempt_number=number, error=err):
            return None
        delay = self.retry.backoff(number - 1, err)
        if self._clock() + delay + MIN_ATTEMPT_BUDGET_S > deadline:
            return None  # waiting would leave no budget for another attempt
        return delay

    def _elapsed_ms(self, started: float) -> float:
        return round((self._clock() - started) * 1000, 2)

    def _record(
        self,
        attempts: list[AttemptRecord],
        number: int,
        outcome: Literal["ok", "error", "rejected"],
        duration_ms: float,
        err: ProviderError | None = None,
        *,
        backoff_s: float = 0.0,
    ) -> None:
        record = AttemptRecord(
            stage=self.name,
            attempt=number,
            outcome=outcome,
            duration_ms=duration_ms,
            backoff_ms=round(backoff_s * 1000, 2),
            error_kind=err.kind.value if err else None,
            status_code=err.status_code if err else None,
            message=err.message if err else None,
        )
        attempts.append(record)
        if self._observer is not None:
            self._observer(self.name, record)
