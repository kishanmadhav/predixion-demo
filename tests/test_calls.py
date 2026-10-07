from voice_agent.calls import CallTracker


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_a_call_is_active_until_ended() -> None:
    tracker = CallTracker(idle_s=45, clock=Clock())
    tracker.touch("a")
    tracker.touch("b")
    assert tracker.active() == 2
    assert tracker.end("a") is True
    assert tracker.end("a") is False
    assert tracker.active() == 1


def test_an_idle_call_stops_counting_after_idle_s() -> None:
    clock = Clock()
    tracker = CallTracker(idle_s=45, clock=clock)
    tracker.touch("a")
    clock.now = 44.9
    assert tracker.active() == 1
    clock.now = 45.1
    assert tracker.active() == 0


def test_turn_counts_in_flight_and_refreshes_the_call() -> None:
    clock = Clock()
    tracker = CallTracker(idle_s=45, clock=clock)
    with tracker.turn("a"):
        assert tracker.in_flight == 1
        clock.now = 100
    assert tracker.in_flight == 0
    clock.now = 140
    assert tracker.active() == 1  # touched again when the turn finished at t=100
