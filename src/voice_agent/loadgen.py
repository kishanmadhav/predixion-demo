"""Traffic generator and chaos scenario for a running service + mock provider.

`loadtest` drives synthetic calls through the HTTP API and reports outcomes from
both sides: what the service says happened and what the provider actually received.
`chaos_demo` walks through baseline flakiness -> hard LLM outage -> recovery -> DLQ
replay, printing breaker state and provider traffic at each step.
"""

from __future__ import annotations

import asyncio
import base64
import statistics
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import httpx

STAGES = ("stt", "llm", "tts")


@dataclass
class LoadReport:
    statuses: Counter[str] = field(default_factory=Counter)
    actions: Counter[str] = field(default_factory=Counter)
    latencies_ms: list[float] = field(default_factory=list)
    errors: Counter[str] = field(default_factory=Counter)
    http_failures: int = 0

    @property
    def total(self) -> int:
        return sum(self.statuses.values()) + self.http_failures


async def run_load(
    client: httpx.AsyncClient,
    *,
    calls: int,
    turns_per_call: int,
    concurrency: int,
    report: LoadReport | None = None,
) -> LoadReport:
    """Each call is a sequence of turns; up to `concurrency` calls run at once."""
    report = report or LoadReport()
    sem = asyncio.Semaphore(concurrency)

    async def one_call() -> None:
        call_id = f"call-{uuid.uuid4().hex[:8]}"
        async with sem:
            for n in range(turns_per_call):
                audio = base64.b64encode(f"{call_id}-{n}".encode()).decode()
                started = time.perf_counter()
                try:
                    r = await client.post(
                        f"/v1/calls/{call_id}/turns", json={"turn_id": f"t{n}", "audio_b64": audio}
                    )
                except httpx.HTTPError as exc:
                    report.http_failures += 1
                    report.errors[type(exc).__name__] += 1
                    continue
                report.latencies_ms.append((time.perf_counter() - started) * 1000)
                if r.status_code != 200:
                    report.http_failures += 1
                    report.errors[f"HTTP {r.status_code}"] += 1
                    continue
                body = r.json()
                report.statuses[body["status"]] += 1
                report.actions[body["action"]] += 1
                if body["error_kind"]:
                    report.errors[f"{body['failed_stage']}:{body['error_kind']}"] += 1

    await asyncio.gather(*(one_call() for _ in range(calls)))
    return report


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def format_load_report(
    report: LoadReport, health: dict[str, Any], mock: dict[str, Any] | None
) -> str:
    lines = [
        f"turns sent:          {report.total}",
        f"  completed:         {report.statuses.get('completed', 0)}",
        f"  degraded:          {report.statuses.get('degraded', 0)}"
        f"  (handoff: {report.actions.get('handoff', 0)})",
        f"  HTTP failures:     {report.http_failures}   <- should always be 0",
    ]
    if report.latencies_ms:
        lines.append(
            f"latency ms:          p50={statistics.median(report.latencies_ms):.0f}"
            f"  p95={_pct(report.latencies_ms, 0.95):.0f}"
            f"  max={max(report.latencies_ms):.0f}"
        )
    if report.errors:
        lines.append(
            "degradation causes:  " + ", ".join(f"{k}={v}" for k, v in report.errors.most_common())
        )
    lines.append(
        "breakers:            "
        + ", ".join(f"{name}={b['state']}" for name, b in health["breakers"].items())
    )
    lines.append(
        "dead letters:        " + ", ".join(f"{k}={v}" for k, v in health["dead_letters"].items())
    )
    if mock is not None:
        lines.append("provider received (requests / ok / failed):")
        for stage in STAGES:
            s = mock["stages"][stage]
            lines.append(
                f"  {stage}: {s['requests']:>5} / {s['ok']:>5} / {s['requests'] - s['ok']:>5}"
            )
    return "\n".join(lines)


async def loadtest(
    url: str,
    mock_url: str | None,
    *,
    calls: int,
    turns_per_call: int,
    concurrency: int,
    reset_mock: bool,
) -> str:
    async with (
        httpx.AsyncClient(base_url=url, timeout=30) as client,
        httpx.AsyncClient(base_url=mock_url or url, timeout=10) as mock,
    ):
        if mock_url and reset_mock:
            await mock.post("/admin/reset")
        report = await run_load(
            client, calls=calls, turns_per_call=turns_per_call, concurrency=concurrency
        )
        health = (await client.get("/health")).json()
        stats = (await mock.get("/admin/stats")).json() if mock_url else None
    return format_load_report(report, health, stats)


async def chaos_demo(url: str, mock_url: str, *, outage_s: float = 12.0) -> None:
    async with (
        httpx.AsyncClient(base_url=url, timeout=30) as client,
        httpx.AsyncClient(base_url=mock_url, timeout=10) as mock,
    ):

        async def snapshot(label: str) -> None:
            health = (await client.get("/health")).json()
            stats = (await mock.get("/admin/stats")).json()["stages"]
            breakers = " ".join(f"{n}={b['state']:<9}" for n, b in health["breakers"].items())
            traffic = " ".join(f"{s}={stats[s]['requests']:<4}" for s in STAGES)
            pending = health["dead_letters"]["pending"]
            print(
                f"  [{label:<22}] breakers: {breakers} provider requests: {traffic} "
                f"dlq pending={pending}"
            )

        await mock.post("/admin/reset")
        print("1) Baseline: provider fails 20% of requests. Retries absorb it.")
        r = await run_load(client, calls=20, turns_per_call=3, concurrency=10)
        print(f"   completed={r.statuses['completed']} degraded={r.statuses['degraded']}")
        await snapshot("after baseline")

        print(
            f"2) Hard LLM outage for {outage_s:.0f}s. Breaker opens; calls degrade "
            "to a fallback prompt; every failed turn is dead-lettered."
        )
        await mock.post("/admin/chaos", json={"stage": "llm", "outage_s": outage_s})
        deadline = time.monotonic() + outage_s
        tick = 0
        while time.monotonic() < deadline:
            r = await run_load(client, calls=5, turns_per_call=1, concurrency=5)
            tick += 1
            if tick % 2 == 1:
                await snapshot(f"outage t+{outage_s - (deadline - time.monotonic()):.0f}s")
            await asyncio.sleep(1.0)

        print(
            "3) Outage over. After its cool-down the breaker half-opens, a few probe "
            "requests succeed, and it closes."
        )
        started = time.monotonic()
        while time.monotonic() - started < 30:
            await run_load(client, calls=5, turns_per_call=1, concurrency=5)
            await snapshot(f"recovery t+{time.monotonic() - started:.0f}s")
            breakers = (await client.get("/health")).json()["breakers"]
            if all(b["state"] == "closed" for b in breakers.values()):
                break
            await asyncio.sleep(1.0)

        print("4) Replay the dead-letter queue now that the provider is healthy.")
        response = await client.post("/v1/dlq/replay", params={"limit": 1000}, timeout=300)
        replay = response.json()
        outcomes = Counter(o["status"] for o in replay["results"])
        print(f"   replay results: {dict(outcomes)}")
        await snapshot("after replay")
