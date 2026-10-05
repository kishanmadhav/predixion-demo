"""End-to-end: the real service wiring against the real mock provider, in-process.

These tests check behaviour from the *downstream* side too (what the provider
actually received), so they demonstrate that retries and circuit breaking happen,
not just that the service claims they do.
"""

import asyncio
import base64
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from mock_provider.app import MockConfig, create_app
from voice_agent.config import Settings
from voice_agent.store import TurnStatus
from voice_agent.wiring import Runtime, open_runtime

MOCK_URL = "http://mock/v1"


def e2e_settings(tmp_path: Path, **overrides: Any) -> Settings:
    params: dict[str, Any] = {
        "_env_file": None,
        "db_path": tmp_path / "e2e.db",
        "stt_base_url": MOCK_URL,
        "llm_base_url": MOCK_URL,
        "tts_base_url": MOCK_URL,
        "stt_timeout_s": 0.25,
        "llm_timeout_s": 0.25,
        "tts_timeout_s": 0.25,
        "turn_deadline_s": 3.0,
        "retry_base_delay_s": 0.01,
        "retry_max_delay_s": 0.05,
    }
    params.update(overrides)
    return Settings(**params)


class Harness:
    def __init__(self, runtime: Runtime, mock: httpx.AsyncClient) -> None:
        self.runtime = runtime
        self.mock = mock

    async def turns(self, n: int, *, prefix: str, concurrency: int = 10) -> list[Any]:
        sem = asyncio.Semaphore(concurrency)

        async def one(i: int) -> Any:
            async with sem:
                audio = f"{prefix}-{i}".encode()
                return await self.runtime.pipeline.handle_turn(f"{prefix}-call-{i % 7}",
                                                               f"turn-{i}", audio)

        return await asyncio.gather(*(one(i) for i in range(n)))

    async def mock_stats(self) -> dict[str, Any]:
        stats: dict[str, Any] = (await self.mock.get("/admin/stats")).json()["stages"]
        return stats

    async def chaos(self, **body: Any) -> None:
        (await self.mock.post("/admin/chaos", json=body)).raise_for_status()


async def make_harness(
    tmp_path: Path, mock_config: MockConfig, **settings: Any
) -> AsyncIterator[Harness]:
    mock_app = create_app(mock_config)
    transport = httpx.ASGITransport(app=mock_app)
    async with (
        httpx.AsyncClient(transport=transport) as provider_client,
        httpx.AsyncClient(transport=transport, base_url="http://mock") as admin,
    ):
        runtime = await open_runtime(e2e_settings(tmp_path, **settings), client=provider_client)
        try:
            yield Harness(runtime, admin)
        finally:
            await runtime.close()


@pytest.fixture
async def flaky(tmp_path: Path) -> AsyncIterator[Harness]:
    """The brief's scenario: 20% of provider requests fail."""
    config = MockConfig(failure_rate=0.2, seed=2024, latency_scale=0.03, hang_s=1.0)
    async for h in make_harness(tmp_path, config):
        yield h


@pytest.fixture
async def outage_prone(tmp_path: Path) -> AsyncIterator[Harness]:
    config = MockConfig(failure_rate=0.0, seed=7, latency_scale=0.02, hang_s=1.0)
    async for h in make_harness(tmp_path, config, breaker_open_s=0.3,
                                breaker_minimum_calls=10, breaker_window=20):
        yield h


async def test_no_turn_is_lost_under_twenty_percent_failure(flaky: Harness) -> None:
    results = await flaky.turns(150, prefix="flaky")

    statuses = [r.status for r in results]
    assert set(statuses) <= {TurnStatus.COMPLETED, TurnStatus.DEGRADED}
    degraded = [r for r in results if r.status is TurnStatus.DEGRADED]
    # Every degraded turn has a dead letter; nothing is left in progress.
    assert all(r.dlq_id is not None for r in degraded)
    counts = await flaky.runtime.store.dead_letter_counts()
    assert counts["pending"] == len(degraded)
    for r in results:
        turn = await flaky.runtime.store.get_turn(r.call_id, r.turn_id)
        assert turn is not None and turn.status is r.status
    # Retries absorb most of the 20% failure rate: 0.2^3 = 0.8% per stage.
    assert len(degraded) / len(results) < 0.10


async def test_retries_are_visible_from_the_provider_side(flaky: Harness) -> None:
    results = await flaky.turns(100, prefix="retry")
    stats = await flaky.mock_stats()
    completed = sum(r.status is TurnStatus.COMPLETED for r in results)
    for stage in ("stt", "llm", "tts"):
        failures = stats[stage]["requests"] - stats[stage]["ok"]
        assert failures > 0
        # Each completed turn needed one OK per stage; failures were retried.
        assert stats[stage]["ok"] >= completed
        assert stats[stage]["requests"] > completed
    # The breaker stays closed at the baseline 20% failure rate.
    assert all(s.breaker.state == "closed" for s in flaky.runtime.stages.values())


@pytest.fixture
async def long_outage(tmp_path: Path) -> AsyncIterator[Harness]:
    # Cool-down far longer than the test, so no half-open probes happen mid-test.
    config = MockConfig(failure_rate=0.0, seed=7, latency_scale=0.02, hang_s=1.0)
    async for h in make_harness(tmp_path, config, breaker_open_s=60,
                                breaker_minimum_calls=10, breaker_window=20):
        yield h


async def test_circuit_opens_during_outage_and_sheds_load(long_outage: Harness) -> None:
    h = long_outage
    await h.chaos(stage="llm", outage_s=30)

    first = await h.turns(10, prefix="warm", concurrency=1)  # trips the breaker
    assert h.runtime.stages["llm"].breaker.state == "open"
    llm_requests_when_opened = (await h.mock_stats())["llm"]["requests"]

    shed = await h.turns(30, prefix="shed", concurrency=5)
    llm_requests_after = (await h.mock_stats())["llm"]["requests"]

    # While open, turns fail fast without touching the LLM at all ...
    assert llm_requests_after == llm_requests_when_opened
    assert all(r.error_kind == "circuit_open" for r in shed)
    # ... but every one of them is still degraded gracefully and dead-lettered.
    assert all(r.status is TurnStatus.DEGRADED and r.dlq_id for r in first + shed)
    # STT still ran for every turn; its breaker is independent.
    assert h.runtime.stages["stt"].breaker.state == "closed"


async def test_circuit_recovers_after_the_outage(outage_prone: Harness) -> None:
    h = outage_prone
    await h.chaos(stage="llm", outage_s=30)
    await h.turns(10, prefix="trip", concurrency=1)
    assert h.runtime.stages["llm"].breaker.state == "open"

    await h.chaos(stage="llm", outage_s=0)  # provider comes back
    await asyncio.sleep(0.4)  # breaker cool-down (0.3s) elapses
    assert h.runtime.stages["llm"].breaker.state == "half_open"
    after = await h.turns(10, prefix="recovered", concurrency=1)

    assert h.runtime.stages["llm"].breaker.state == "closed"
    assert all(r.status is TurnStatus.COMPLETED for r in after)


async def test_dead_letters_from_an_outage_replay_cleanly_after_recovery(
    outage_prone: Harness,
) -> None:
    h = outage_prone
    await h.chaos(stage="tts", outage_s=30)
    failed = await h.turns(12, prefix="dl", concurrency=3)
    assert all(r.status is TurnStatus.DEGRADED for r in failed)

    await h.chaos(stage="tts", outage_s=0)
    await asyncio.sleep(0.4)
    outcomes = await h.runtime.dlq.replay_pending()

    assert [o.status for o in outcomes] == ["resolved"] * 12
    for r in failed:
        turn = await h.runtime.store.get_turn(r.call_id, r.turn_id)
        assert turn is not None and turn.status is TurnStatus.COMPLETED_ON_REPLAY
        assert turn.result and turn.result["reply_text"]


async def test_hung_provider_is_cut_off_by_the_attempt_timeout(tmp_path: Path) -> None:
    config = MockConfig(failure_rate=0.0, latency_scale=0.0, hang_s=30)
    async for h in make_harness(tmp_path, config):
        await h.chaos(stage="stt", failure_rate=1.0)  # mix includes hangs
        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await h.runtime.pipeline.handle_turn("c", "t", base64.b64encode(b"x"))
        assert loop.time() - started < 3.5  # bounded by the turn deadline, not hang_s
        assert result.status is TurnStatus.DEGRADED
