"""Circuit breaker: closed -> open -> half-open -> closed.

* CLOSED: calls flow; outcomes go into a rolling window of the last `window_size`
  counted calls. When a failure leaves at least `minimum_calls` in the window with a
  failure rate at or above `failure_rate_threshold`, the circuit opens.
* OPEN: calls are rejected immediately with `ErrorKind.CIRCUIT_OPEN` (no load on the
  struggling provider, no latency for the caller) for `open_seconds`.
* HALF_OPEN: up to `half_open_max_calls` probes are admitted. The majority decides:
  enough failures re-open the circuit (with a fresh cool-down), enough successes close
  it with an empty window.

Every permit carries the breaker's epoch (bumped on each transition). An outcome
from a call admitted under a previous state is ignored, so a slow call that started
before the circuit opened cannot be miscounted as a half-open probe.

The breaker is not thread-safe; it is designed for a single asyncio event loop,
where no other coroutine can run between its statements.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from voice_agent.providers.base import ErrorKind, ProviderError


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


TransitionCallback = Callable[[str, BreakerState, BreakerState], None]


@dataclass(frozen=True, slots=True)
class Permit:
    epoch: int


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        *,
        window_size: int = 50,
        minimum_calls: int = 20,
        failure_rate_threshold: float = 0.6,
        open_seconds: float = 5.0,
        half_open_max_calls: int = 5,
        clock: Callable[[], float] = time.monotonic,
        on_transition: TransitionCallback | None = None,
    ) -> None:
        if window_size < 1:
            raise ValueError("window_size must be >= 1")
        if not 1 <= minimum_calls <= window_size:
            raise ValueError("minimum_calls must be between 1 and window_size")
        if not 0 < failure_rate_threshold <= 1:
            raise ValueError("failure_rate_threshold must be in (0, 1]")
        if half_open_max_calls < 1:
            raise ValueError("half_open_max_calls must be >= 1")
        if open_seconds < 0:
            raise ValueError("open_seconds must be >= 0")

        self.name = name
        self._minimum_calls = minimum_calls
        self._threshold = failure_rate_threshold
        self._open_seconds = open_seconds
        self._half_open_max = half_open_max_calls
        self._clock = clock
        self._on_transition = on_transition

        self._state = BreakerState.CLOSED
        self._epoch = 0
        self._window: deque[bool] = deque(maxlen=window_size)  # True = failure
        self._opened_at = 0.0
        self._probes_in_flight = 0
        self._probe_successes = 0
        self._probe_failures = 0

    # -- public API ---------------------------------------------------------

    @property
    def state(self) -> BreakerState:
        self._maybe_half_open()
        return self._state

    def acquire(self) -> Permit:
        """Admit a call or raise `ProviderError(kind=CIRCUIT_OPEN)`."""
        self._maybe_half_open()
        if self._state is BreakerState.OPEN:
            remaining = self._opened_at + self._open_seconds - self._clock()
            raise ProviderError(
                self.name, ErrorKind.CIRCUIT_OPEN, f"circuit open, next probe in {remaining:.1f}s"
            )
        if self._state is BreakerState.HALF_OPEN:
            decided = self._probe_successes + self._probe_failures
            if self._probes_in_flight + decided >= self._half_open_max:
                raise ProviderError(
                    self.name, ErrorKind.CIRCUIT_OPEN, "circuit half-open, probe limit reached"
                )
            self._probes_in_flight += 1
        return Permit(self._epoch)

    def on_success(self, permit: Permit) -> None:
        self._record(permit, failed=False)

    def on_failure(self, permit: Permit) -> None:
        self._record(permit, failed=True)

    def release(self, permit: Permit) -> None:
        """Return a permit whose call said nothing about provider health (e.g. a 400)."""
        if permit.epoch == self._epoch and self._state is BreakerState.HALF_OPEN:
            self._probes_in_flight -= 1

    def snapshot(self) -> dict[str, Any]:
        state = self.state
        snap: dict[str, Any] = {
            "state": state.value,
            "failure_rate": self._failure_rate(),
            "calls_in_window": len(self._window),
        }
        if state is BreakerState.OPEN:
            snap["seconds_until_half_open"] = round(
                max(0.0, self._opened_at + self._open_seconds - self._clock()), 3
            )
        return snap

    # -- internals ----------------------------------------------------------

    def _record(self, permit: Permit, *, failed: bool) -> None:
        if permit.epoch != self._epoch:
            return  # admitted under a previous state; its outcome is stale
        if self._state is BreakerState.CLOSED:
            self._window.append(failed)
            # Only a failure can trip the circuit: opening right after a healthy call
            # would cut off a provider that is already recovering.
            if (
                failed
                and len(self._window) >= self._minimum_calls
                and self._failure_rate() >= self._threshold
            ):
                self._transition(BreakerState.OPEN)
        elif self._state is BreakerState.HALF_OPEN:
            self._probes_in_flight -= 1
            if failed:
                self._probe_failures += 1
            else:
                self._probe_successes += 1
            if self._probe_failures / self._half_open_max >= self._threshold:
                self._transition(BreakerState.OPEN)
            elif self._probe_successes / self._half_open_max > 1 - self._threshold:
                self._transition(BreakerState.CLOSED)

    def _failure_rate(self) -> float:
        if not self._window:
            return 0.0
        return sum(self._window) / len(self._window)

    def _maybe_half_open(self) -> None:
        if (
            self._state is BreakerState.OPEN
            and self._clock() >= self._opened_at + self._open_seconds
        ):
            self._transition(BreakerState.HALF_OPEN)

    def _transition(self, new: BreakerState) -> None:
        old = self._state
        self._state = new
        self._epoch += 1
        self._probes_in_flight = self._probe_successes = self._probe_failures = 0
        if new is BreakerState.OPEN:
            self._opened_at = self._clock()
        elif new is BreakerState.CLOSED:
            self._window.clear()
        if self._on_transition is not None:
            self._on_transition(self.name, old, new)
