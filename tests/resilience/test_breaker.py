import pytest

from voice_agent.providers.base import ErrorKind, ProviderError
from voice_agent.resilience.breaker import BreakerState, CircuitBreaker


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def transitions() -> list[tuple[str, BreakerState, BreakerState]]:
    return []


@pytest.fixture
def breaker(
    clock: FakeClock, transitions: list[tuple[str, BreakerState, BreakerState]]
) -> CircuitBreaker:
    return CircuitBreaker(
        "llm",
        window_size=10,
        minimum_calls=5,
        failure_rate_threshold=0.5,
        open_seconds=5.0,
        half_open_max_calls=3,
        clock=clock,
        on_transition=lambda name, old, new: transitions.append((name, old, new)),
    )


def succeed(b: CircuitBreaker, n: int = 1) -> None:
    for _ in range(n):
        b.on_success(b.acquire())


def fail(b: CircuitBreaker, n: int = 1) -> None:
    for _ in range(n):
        b.on_failure(b.acquire())


def trip(b: CircuitBreaker) -> None:
    fail(b, 5)
    assert b.state is BreakerState.OPEN


def test_starts_closed(breaker: CircuitBreaker) -> None:
    assert breaker.state is BreakerState.CLOSED


def test_does_not_trip_before_minimum_calls(breaker: CircuitBreaker) -> None:
    fail(breaker, 4)
    assert breaker.state is BreakerState.CLOSED


def test_trips_when_failure_rate_reaches_threshold(
    breaker: CircuitBreaker, transitions: list[tuple[str, BreakerState, BreakerState]]
) -> None:
    succeed(breaker, 3)
    fail(breaker, 2)
    assert breaker.state is BreakerState.CLOSED  # 2/5 = 40%
    fail(breaker, 1)  # 3/6 = 50%
    assert breaker.state is BreakerState.OPEN
    assert transitions == [("llm", BreakerState.CLOSED, BreakerState.OPEN)]


def test_a_success_never_opens_the_circuit(breaker: CircuitBreaker) -> None:
    fail(breaker, 4)  # 4 of 4 failed, but below minimum_calls
    succeed(breaker, 1)  # reaches minimum_calls at 80% failure: the latest call was healthy
    assert breaker.state is BreakerState.CLOSED
    fail(breaker, 1)
    assert breaker.state is BreakerState.OPEN


def test_baseline_flakiness_below_threshold_keeps_it_closed(breaker: CircuitBreaker) -> None:
    # 20% failure, evenly spread: the normal "bad day" level must not open the circuit.
    for _ in range(20):
        succeed(breaker, 4)
        fail(breaker, 1)
    assert breaker.state is BreakerState.CLOSED


def test_window_forgets_old_outcomes(breaker: CircuitBreaker) -> None:
    succeed(breaker, 3)
    fail(breaker, 2)
    succeed(breaker, 10)  # the two failures have rolled out of the 10-call window
    assert breaker.snapshot()["failure_rate"] == 0.0
    fail(breaker, 4)
    assert breaker.state is BreakerState.CLOSED  # 4/10
    fail(breaker, 1)
    assert breaker.state is BreakerState.OPEN  # 5/10


def test_open_circuit_rejects_fast_with_circuit_open_error(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    trip(breaker)
    clock.advance(4.9)
    with pytest.raises(ProviderError) as exc:
        breaker.acquire()
    assert exc.value.kind is ErrorKind.CIRCUIT_OPEN
    assert exc.value.stage == "llm"
    assert not exc.value.retryable


def test_moves_to_half_open_after_cool_down(breaker: CircuitBreaker, clock: FakeClock) -> None:
    trip(breaker)
    clock.advance(5.0)
    assert breaker.state is BreakerState.HALF_OPEN


def test_half_open_admits_only_the_probe_limit(breaker: CircuitBreaker, clock: FakeClock) -> None:
    trip(breaker)
    clock.advance(5.0)
    permits = [breaker.acquire() for _ in range(3)]
    with pytest.raises(ProviderError) as exc:
        breaker.acquire()
    assert exc.value.kind is ErrorKind.CIRCUIT_OPEN
    assert len(permits) == 3


def test_healthy_probes_close_the_circuit_and_reset_the_window(
    breaker: CircuitBreaker,
    clock: FakeClock,
    transitions: list[tuple[str, BreakerState, BreakerState]],
) -> None:
    trip(breaker)
    clock.advance(5.0)
    succeed(breaker, 2)
    assert breaker.state is BreakerState.CLOSED  # 2 of 3 probes healthy: majority decided
    assert [t[1:] for t in transitions] == [
        (BreakerState.CLOSED, BreakerState.OPEN),
        (BreakerState.OPEN, BreakerState.HALF_OPEN),
        (BreakerState.HALF_OPEN, BreakerState.CLOSED),
    ]
    fail(breaker, 4)  # pre-trip failures were forgotten; 4 < minimum_calls
    assert breaker.state is BreakerState.CLOSED


def test_one_flaky_probe_does_not_reopen(breaker: CircuitBreaker, clock: FakeClock) -> None:
    trip(breaker)
    clock.advance(5.0)
    fail(breaker, 1)
    assert breaker.state is BreakerState.HALF_OPEN
    succeed(breaker, 2)
    assert breaker.state is BreakerState.CLOSED


def test_failing_probes_reopen_the_circuit(breaker: CircuitBreaker, clock: FakeClock) -> None:
    trip(breaker)
    clock.advance(5.0)
    fail(breaker, 2)
    assert breaker.state is BreakerState.OPEN
    clock.advance(4.0)
    with pytest.raises(ProviderError):
        breaker.acquire()  # a fresh cool-down started at the re-open


def test_released_probe_frees_its_slot(breaker: CircuitBreaker, clock: FakeClock) -> None:
    trip(breaker)
    clock.advance(5.0)
    permits = [breaker.acquire() for _ in range(3)]
    breaker.release(permits[0])  # e.g. the probe got a 400: says nothing about health
    breaker.acquire()  # does not raise


def test_outcomes_from_a_previous_state_are_ignored(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    stale = [breaker.acquire() for _ in range(3)]  # in flight while the breaker trips
    trip(breaker)
    clock.advance(5.0)
    for permit in stale:
        breaker.on_failure(permit)
    assert breaker.state is BreakerState.HALF_OPEN


def test_snapshot_reports_state_and_failure_rate(breaker: CircuitBreaker) -> None:
    succeed(breaker, 3)
    fail(breaker, 1)
    snap = breaker.snapshot()
    assert snap["state"] == "closed"
    assert snap["failure_rate"] == pytest.approx(0.25)
    assert snap["calls_in_window"] == 4


@pytest.mark.parametrize(
    "kwargs",
    [
        {"window_size": 0},
        {"minimum_calls": 11},
        {"failure_rate_threshold": 0},
        {"failure_rate_threshold": 1.5},
        {"half_open_max_calls": 0},
        {"open_seconds": -1},
    ],
)
def test_invalid_configuration_is_rejected(kwargs: dict[str, float]) -> None:
    params: dict[str, float] = {"window_size": 10, "minimum_calls": 5}
    params.update(kwargs)
    with pytest.raises(ValueError):
        CircuitBreaker("x", **params)  # type: ignore[arg-type]
