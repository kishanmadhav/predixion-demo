"""Scripted campaign traffic: a flat baseline, a campaign-window spike, a cool-down.

Calls arrive as a Poisson process at each phase's rate. A call is `turns_per_call`
turns, `gap_s` apart, each carrying a real WAV utterance, and it ends with
POST .../end so the service stops counting it. Each call keeps the ALB stickiness
cookie, so all of its turns land on one task, as a WebSocket per call would.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import random
import statistics
import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from http.cookiejar import CookieJar, DefaultCookiePolicy
from pathlib import Path
from typing import Any

import httpx


@dataclass(frozen=True)
class Phase:
    name: str
    minutes: float
    calls_per_min: float


@dataclass
class PhaseStats:
    calls_started: int = 0
    turns: Counter[str] = field(default_factory=Counter)
    http_failures: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    errors: Counter[str] = field(default_factory=Counter)

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls_started": self.calls_started,
            "turns": dict(self.turns),
            "http_failures": self.http_failures,
            "errors": dict(self.errors),
            "latency_ms": latency_summary(self.latencies_ms),
        }


def parse_profile(text: str) -> list[Phase]:
    phases = []
    for part in text.split(","):
        pieces = [p.strip() for p in part.split(":")]
        if len(pieces) != 3:
            raise ValueError(f"phase {part!r} is not name:minutes:calls_per_min")
        phases.append(Phase(pieces[0], float(pieces[1]), float(pieces[2])))
    return phases


def load_audio(directory: Path) -> list[bytes]:
    files = sorted(directory.glob("*.wav"))
    if not files:
        raise FileNotFoundError(f"no .wav files in {directory}; run scripts/make_utterances.ps1")
    return [f.read_bytes() for f in files]


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def latency_summary(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    return {
        "p50": round(statistics.median(values)),
        "p90": round(percentile(values, 0.90)),
        "p99": round(percentile(values, 0.99)),
        "max": round(max(values)),
    }


def make_client(
    base_url: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    """A client that never stores cookies. Each call tracks its own ALB stickiness
    cookie; a shared jar would pin every call to whichever task answered first."""
    no_cookies = CookieJar(policy=DefaultCookiePolicy(allowed_domains=[]))
    return httpx.AsyncClient(
        base_url=base_url,
        transport=transport,
        cookies=no_cookies,
        timeout=60,
        limits=httpx.Limits(max_connections=400, max_keepalive_connections=100),
    )


def _remember_stickiness(response: httpx.Response, jar: dict[str, str]) -> None:
    for raw in response.headers.get_list("set-cookie"):
        name, _, rest = raw.partition("=")
        if name.strip().startswith("AWSALB"):
            jar[name.strip()] = rest.split(";", 1)[0]


def _cookie(jar: dict[str, str]) -> dict[str, str]:
    return {"cookie": "; ".join(f"{k}={v}" for k, v in jar.items())} if jar else {}


class Campaign:
    def __init__(
        self,
        client: httpx.AsyncClient,
        phases: list[Phase],
        *,
        audio: list[bytes],
        turns_per_call: int = 3,
        gap_s: float = 30.0,
        rng: random.Random | None = None,
        progress_every_s: float = 30.0,
        out: Callable[[str], None] = print,
    ) -> None:
        self._client = client
        self._phases = phases
        self._audio = [base64.b64encode(a).decode("ascii") for a in audio]
        self._turns = turns_per_call
        self._gap_s = gap_s
        self._rng = rng or random.Random()
        self._progress_every_s = progress_every_s
        self._out = out
        self.stats: dict[str, PhaseStats] = {}
        self.active = 0
        self._phase = ""

    async def run(self) -> dict[str, PhaseStats]:
        started = time.monotonic()
        calls: set[asyncio.Task[None]] = set()
        progress = asyncio.create_task(self._progress(started))
        try:
            for phase in self._phases:
                self._phase = phase.name
                stats = self.stats[phase.name] = PhaseStats()
                phase_end = time.monotonic() + phase.minutes * 60
                rate_per_s = phase.calls_per_min / 60
                while True:
                    gap = self._rng.expovariate(rate_per_s) if rate_per_s > 0 else float("inf")
                    if time.monotonic() + gap >= phase_end:
                        await asyncio.sleep(max(0.0, phase_end - time.monotonic()))
                        break
                    await asyncio.sleep(gap)
                    task = asyncio.create_task(self._call(stats))
                    calls.add(task)
                    task.add_done_callback(calls.discard)
            self._phase = "draining"
            if calls:
                await asyncio.gather(*calls, return_exceptions=True)
        finally:
            for pending in list(calls):
                pending.cancel()
            if calls:
                await asyncio.gather(*calls, return_exceptions=True)
            progress.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await progress
        return self.stats

    async def _call(self, stats: PhaseStats) -> None:
        call_id = f"call-{uuid.uuid4().hex[:10]}"
        jar: dict[str, str] = {}
        stats.calls_started += 1
        self.active += 1
        try:
            for n in range(self._turns):
                if n:
                    await asyncio.sleep(self._gap_s)
                body = {"turn_id": f"t{n}", "audio_b64": self._rng.choice(self._audio)}
                sent = time.perf_counter()
                try:
                    r = await self._client.post(
                        f"/v1/calls/{call_id}/turns", json=body, headers=_cookie(jar)
                    )
                except httpx.HTTPError as exc:
                    stats.http_failures += 1
                    stats.errors[type(exc).__name__] += 1
                    continue
                elapsed_ms = (time.perf_counter() - sent) * 1000
                _remember_stickiness(r, jar)
                if r.status_code != 200:
                    stats.http_failures += 1
                    stats.errors[f"HTTP {r.status_code}"] += 1
                    continue
                stats.latencies_ms.append(elapsed_ms)
                try:
                    data = r.json()
                    status = data["status"]
                    error_kind = data.get("error_kind")
                    failed_stage = data.get("failed_stage")
                except (ValueError, KeyError, AttributeError):
                    stats.http_failures += 1
                    stats.errors["bad response"] += 1
                    continue
                stats.turns[status] += 1
                if error_kind:
                    stats.errors[f"{failed_stage}:{error_kind}"] += 1
            with contextlib.suppress(httpx.HTTPError):
                await self._client.post(f"/v1/calls/{call_id}/end", headers=_cookie(jar))
        finally:
            self.active -= 1

    async def _progress(self, started: float) -> None:
        while True:
            await asyncio.sleep(self._progress_every_s)
            stats = self.stats.get(self._phase) or PhaseStats()
            recent = stats.latencies_ms[-200:]
            done = sum(stats.turns.values())
            self._out(
                f"t+{(time.monotonic() - started) / 60:5.1f}m  phase={self._phase:<9} "
                f"active_calls={self.active:<4} turns={done:<5} "
                f"degraded={done - stats.turns['completed']:<4} http_fail={stats.http_failures:<3} "
                f"p50={percentile(recent, 0.5):.0f}ms p99={percentile(recent, 0.99):.0f}ms"
            )


def format_campaign(stats: dict[str, PhaseStats]) -> str:
    lines = [
        f"{'phase':<10} {'calls':>6} {'turns':>6} {'completed':>9} {'degraded':>8} "
        f"{'http_fail':>9} {'p50':>6} {'p90':>6} {'p99':>6}"
    ]
    for name, s in stats.items():
        total = sum(s.turns.values())
        lat = latency_summary(s.latencies_ms)
        lines.append(
            f"{name:<10} {s.calls_started:>6} {total:>6} {s.turns['completed']:>9} "
            f"{total - s.turns['completed']:>8} {s.http_failures:>9} "
            f"{lat.get('p50', 0):>6.0f} {lat.get('p90', 0):>6.0f} {lat.get('p99', 0):>6.0f}"
        )
    return "\n".join(lines)
