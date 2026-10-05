import asyncio
import random
import time
from collections.abc import Awaitable, Callable

import pytest

from voice_agent.providers.base import ErrorKind, ProviderError
from voice_agent.resilience.breaker import BreakerState, CircuitBreaker
from voice_agent.resilience.retry import RetryPolicy
from voice_agent.resilience.stage import AttemptRecord, ResilientStage, StageFailedError


class FakeTime:
    """Clock + sleep pair: sleeping advances the clock instantly."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def scripted(*steps: object) -> tuple[Callable[[], Awaitable[str]], list[int]]:
    """Each call pops the next step: an exception is raised, anything else returned."""
    queue = list(steps)
    calls = [0]

    async def fn() -> str:
        calls[0] += 1
        step = queue.pop(0)
        if isinstance(step, BaseException):
            raise step
        return str(step)

    return fn, calls


def transient(status: int = 503) -> ProviderError:
    return ProviderError("llm", ErrorKind.SERVER_ERROR, "unavailable", status_code=status)


def make_stage(
    t: FakeTime | None = None,
    *,
    breaker: CircuitBreaker | None = None,
    max_attempts: int = 3,
    timeout_s: float = 1.0,
    records: list[AttemptRecord] | None = None,
) -> ResilientStage:
    t = t or FakeTime()
    return ResilientStage(
        "llm",
        breaker=breaker or CircuitBreaker("llm", window_size=10, minimum_calls=5, clock=t.clock),
        retry=RetryPolicy(
            max_attempts=max_attempts, base_delay=0.1, max_delay=1.0, rng=random.Random(3)
        ),
        timeout_s=timeout_s,
        sleep=t.sleep,
        clock=t.clock,
        observer=(lambda stage, rec: records.append(rec)) if records is not None else None,
    )


async def test_returns_value_on_first_success() -> None:
    fn, calls = scripted("hello")
    outcome = await make_stage().run(fn, deadline=10.0)
    assert outcome.value == "hello"
    assert calls[0] == 1
    assert [a.outcome for a in outcome.attempts] == ["ok"]


async def test_retries_transient_failures_then_succeeds() -> None:
    t = FakeTime()
    fn, calls = scripted(transient(503), transient(500), "ok")
    outcome = await make_stage(t).run(fn, deadline=10.0)
    assert outcome.value == "ok"
    assert calls[0] == 3
    assert [a.outcome for a in outcome.attempts] == ["error", "error", "ok"]
    assert [a.status_code for a in outcome.attempts] == [503, 500, None]
    assert len(t.sleeps) == 2
    assert outcome.attempts[0].backoff_ms == pytest.approx(t.sleeps[0] * 1000, abs=0.01)


async def test_gives_up_after_max_attempts_with_full_attempt_history() -> None:
    fn, calls = scripted(transient(), transient(), transient(), "never reached")
    with pytest.raises(StageFailedError) as exc:
        await make_stage(max_attempts=3).run(fn, deadline=10.0)
    assert calls[0] == 3
    assert exc.value.stage == "llm"
    assert exc.value.error.kind is ErrorKind.SERVER_ERROR
    assert len(exc.value.attempts) == 3
    assert exc.value.attempts[-1].backoff_ms == 0


async def test_does_not_retry_non_retryable_errors() -> None:
    bad = ProviderError("llm", ErrorKind.CLIENT_ERROR, "bad request", status_code=400)
    fn, calls = scripted(bad, "never reached")
    with pytest.raises(StageFailedError) as exc:
        await make_stage().run(fn, deadline=10.0)
    assert calls[0] == 1
    assert exc.value.error.kind is ErrorKind.CLIENT_ERROR


async def test_non_retryable_errors_do_not_count_toward_the_breaker() -> None:
    t = FakeTime()
    breaker = CircuitBreaker("llm", window_size=5, minimum_calls=1, clock=t.clock)
    stage = make_stage(t, breaker=breaker)
    for _ in range(5):
        fn, _ = scripted(ProviderError("llm", ErrorKind.AUTH, "403", status_code=403))
        with pytest.raises(StageFailedError):
            await stage.run(fn, deadline=10.0)
    assert breaker.state is BreakerState.CLOSED


async def test_open_breaker_fails_fast_without_calling_the_provider() -> None:
    t = FakeTime()
    breaker = CircuitBreaker("llm", window_size=5, minimum_calls=1, open_seconds=30, clock=t.clock)
    breaker.on_failure(breaker.acquire())
    assert breaker.state is BreakerState.OPEN
    fn, calls = scripted("never reached")
    with pytest.raises(StageFailedError) as exc:
        await make_stage(t, breaker=breaker).run(fn, deadline=10.0)
    assert calls[0] == 0
    assert exc.value.error.kind is ErrorKind.CIRCUIT_OPEN
    assert [a.outcome for a in exc.value.attempts] == ["rejected"]
    assert t.sleeps == []  # an open circuit is never retried


async def test_breaker_opening_mid_retry_stops_the_retry_loop() -> None:
    t = FakeTime()
    breaker = CircuitBreaker("llm", window_size=5, minimum_calls=2, open_seconds=30, clock=t.clock)
    fn, calls = scripted(transient(), transient(), "never reached")
    with pytest.raises(StageFailedError) as exc:
        await make_stage(t, breaker=breaker, max_attempts=5).run(fn, deadline=10.0)
    assert calls[0] == 2
    assert [a.outcome for a in exc.value.attempts] == ["error", "error", "rejected"]


async def test_hung_provider_times_out_and_is_retried() -> None:
    async def hang() -> str:
        await asyncio.sleep(10)
        return "late"

    attempts = [0]

    async def fn() -> str:
        attempts[0] += 1
        return await hang() if attempts[0] == 1 else "ok"

    stage = ResilientStage(
        "stt",
        breaker=CircuitBreaker("stt"),
        retry=RetryPolicy(max_attempts=2, base_delay=0.0, max_delay=0.0),
        timeout_s=0.05,
    )
    outcome = await stage.run(fn, deadline=time.monotonic() + 5)
    assert outcome.value == "ok"
    assert outcome.attempts[0].error_kind == ErrorKind.TIMEOUT


async def test_attempt_timeout_is_capped_by_the_turn_deadline() -> None:
    async def hang() -> str:
        await asyncio.sleep(10)
        return "late"

    stage = ResilientStage(
        "stt",
        breaker=CircuitBreaker("stt"),
        retry=RetryPolicy(max_attempts=1),
        timeout_s=5.0,
    )
    started = time.monotonic()
    with pytest.raises(StageFailedError) as exc:
        await stage.run(hang, deadline=started + 0.1)
    assert time.monotonic() - started < 1.0
    assert exc.value.error.kind is ErrorKind.TIMEOUT


async def test_no_attempt_is_started_without_enough_deadline_budget() -> None:
    t = FakeTime()
    t.now = 9.99
    fn, calls = scripted("never reached")
    with pytest.raises(StageFailedError) as exc:
        await make_stage(t).run(fn, deadline=10.0)
    assert calls[0] == 0
    assert exc.value.error.kind is ErrorKind.DEADLINE


async def test_does_not_sleep_past_the_deadline() -> None:
    t = FakeTime()
    rate_limited = ProviderError(
        "llm", ErrorKind.RATE_LIMITED, "429", status_code=429, retry_after=1.0
    )
    fn, calls = scripted(rate_limited, "never reached")
    with pytest.raises(StageFailedError) as exc:
        await make_stage(t).run(fn, deadline=0.5)
    assert calls[0] == 1
    assert t.sleeps == []
    assert exc.value.error.kind is ErrorKind.RATE_LIMITED  # the real cause is preserved


async def test_programming_errors_propagate_and_are_not_retried() -> None:
    fn, calls = scripted(KeyError("bug"), "never reached")
    with pytest.raises(KeyError):
        await make_stage().run(fn, deadline=10.0)
    assert calls[0] == 1


async def test_observer_sees_every_attempt() -> None:
    records: list[AttemptRecord] = []
    fn, _ = scripted(transient(), "ok")
    await make_stage(records=records).run(fn, deadline=10.0)
    assert [r.outcome for r in records] == ["error", "ok"]
    assert [r.attempt for r in records] == [1, 2]
