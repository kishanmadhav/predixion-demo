"""Which calls this task is currently holding.

With ALB stickiness every turn of a call lands on the same task, so "active calls on
this task" is the load signal autoscaling targets: a call occupies capacity between
turns too, not just while a turn is being processed. A call stops counting when the
client ends it (POST /v1/calls/{id}/end) or after `idle_s` without a turn.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager


class CallTracker:
    def __init__(self, idle_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._idle_s = idle_s
        self._clock = clock
        self._last_seen: dict[str, float] = {}
        self.in_flight = 0

    def touch(self, call_id: str) -> None:
        self._last_seen[call_id] = self._clock()

    def end(self, call_id: str) -> bool:
        return self._last_seen.pop(call_id, None) is not None

    def active(self) -> int:
        cutoff = self._clock() - self._idle_s
        for call_id in [c for c, seen in self._last_seen.items() if seen < cutoff]:
            del self._last_seen[call_id]
        return len(self._last_seen)

    @contextmanager
    def turn(self, call_id: str) -> Iterator[None]:
        self.touch(call_id)
        self.in_flight += 1
        try:
            yield
        finally:
            self.in_flight -= 1
            self.touch(call_id)
