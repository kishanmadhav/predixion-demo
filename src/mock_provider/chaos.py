"""Failure-injection core shared by the mock provider and the chaos proxy."""

from __future__ import annotations

import math
import os
import random
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import FastAPI
from pydantic import BaseModel, Field

Stage = Literal["stt", "llm", "tts"]
STAGES: tuple[Stage, ...] = ("stt", "llm", "tts")

FAILURE_MODES: tuple[tuple[str, float], ...] = (
    ("http_503", 0.40),
    ("http_500", 0.20),
    ("http_429", 0.15),
    ("hang", 0.15),
    ("malformed", 0.10),
)
OUTCOMES = ("ok", *(mode for mode, _ in FAILURE_MODES), "outage", "upstream_error")

# (median seconds, lognormal sigma)
LATENCY: dict[Stage, tuple[float, float]] = {
    "stt": (0.150, 0.35),
    "llm": (0.400, 0.40),
    "tts": (0.200, 0.35),
}


@dataclass
class MockConfig:
    failure_rate: float = 0.2
    seed: int | None = None
    latency_scale: float = 1.0
    hang_s: float = 10.0

    @classmethod
    def from_env(cls) -> MockConfig:
        seed = os.environ.get("MOCK_SEED")
        return cls(
            failure_rate=float(os.environ.get("MOCK_FAILURE_RATE", "0.2")),
            seed=int(seed) if seed else None,
            latency_scale=float(os.environ.get("MOCK_LATENCY_SCALE", "1.0")),
            hang_s=float(os.environ.get("MOCK_HANG_S", "10.0")),
        )


@dataclass
class ChaosState:
    config: MockConfig
    rng: random.Random
    failure_rates: dict[str, float]
    outage_until: dict[str, float] = field(default_factory=dict)
    stats: dict[str, Counter[str]] = field(default_factory=lambda: {s: Counter() for s in STAGES})

    def reset(self) -> None:
        self.failure_rates = dict.fromkeys(STAGES, self.config.failure_rate)
        self.outage_until.clear()
        self.stats = {s: Counter() for s in STAGES}

    def pick_outcome(self, stage: Stage) -> str:
        if self.outage_until.get(stage, 0.0) > time.monotonic():
            return "outage"
        if self.rng.random() >= self.failure_rates[stage]:
            return "ok"
        roll, cumulative = self.rng.random(), 0.0
        for mode, weight in FAILURE_MODES:
            cumulative += weight
            if roll < cumulative:
                return mode
        return FAILURE_MODES[-1][0]

    def latency(self, stage: Stage) -> float:
        median, sigma = LATENCY[stage]
        return self.rng.lognormvariate(math.log(median), sigma) * self.config.latency_scale


class ChaosRequest(BaseModel):
    stage: Stage | None = None
    failure_rate: float | None = Field(None, ge=0, le=1)
    outage_s: float | None = Field(None, ge=0)


def add_admin_routes(app: FastAPI, state: ChaosState) -> None:
    @app.post("/admin/chaos")
    async def chaos(req: ChaosRequest) -> dict[str, Any]:
        targets = [req.stage] if req.stage else list(STAGES)
        for stage in targets:
            if req.failure_rate is not None:
                state.failure_rates[stage] = req.failure_rate
            if req.outage_s is not None:
                state.outage_until[stage] = time.monotonic() + req.outage_s
        return await stats()

    @app.post("/admin/reset")
    async def reset() -> dict[str, Any]:
        state.reset()
        return await stats()

    @app.get("/admin/stats")
    async def stats() -> dict[str, Any]:
        now = time.monotonic()
        return {
            "failure_rates": dict(state.failure_rates),
            "outages": {
                s: round(until - now, 2) for s, until in state.outage_until.items() if until > now
            },
            "stages": {
                s: {"requests": c["requests"], **{o: c[o] for o in OUTCOMES}}
                for s, c in state.stats.items()
            },
        }
