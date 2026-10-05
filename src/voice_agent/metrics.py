"""Prometheus metrics. Each runtime owns its own registry (no global state)."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

from voice_agent.resilience.breaker import BreakerState
from voice_agent.resilience.stage import AttemptRecord

BREAKER_STATE_VALUE = {BreakerState.CLOSED: 0, BreakerState.HALF_OPEN: 1, BreakerState.OPEN: 2}


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        r = self.registry
        self.attempts = Counter(
            "provider_attempts", "Calls attempted against a provider, by outcome",
            ["stage", "outcome", "error_kind"], registry=r,
        )
        self.retries = Counter(
            "provider_retries", "Attempts beyond the first for a stage", ["stage"], registry=r
        )
        self.breaker_state = Gauge(
            "circuit_breaker_state", "0=closed, 1=half_open, 2=open", ["stage"], registry=r
        )
        self.breaker_transitions = Counter(
            "circuit_breaker_transitions", "Circuit breaker state changes",
            ["stage", "from_state", "to_state"], registry=r,
        )
        self.turns = Counter("turns", "Turns handled, by final status", ["status"], registry=r)
        self.turn_seconds = Histogram(
            "turn_duration_seconds", "End-to-end turn latency", ["status"], registry=r,
            buckets=(0.25, 0.5, 0.75, 1, 1.5, 2, 3, 4, 6, 8),
        )
        self.dead_letters = Counter(
            "dead_letters", "Turns written to the dead-letter queue", ["reason"], registry=r
        )
        self.replays = Counter("dlq_replays", "Dead-letter replays, by result", ["result"],
                               registry=r)

    def observe_attempt(self, stage: str, record: AttemptRecord) -> None:
        self.attempts.labels(stage, record.outcome, record.error_kind or "").inc()
        if record.attempt > 1 and record.outcome != "rejected":
            self.retries.labels(stage).inc()

    def observe_transition(self, stage: str, old: BreakerState, new: BreakerState) -> None:
        self.breaker_state.labels(stage).set(BREAKER_STATE_VALUE[new])
        self.breaker_transitions.labels(stage, old.value, new.value).inc()

    def render(self) -> bytes:
        return generate_latest(self.registry)
