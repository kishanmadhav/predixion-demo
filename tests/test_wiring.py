import asyncio
import contextlib
from pathlib import Path
from typing import Any

import pytest

from tests.fakes import fake_providers, fast_settings
from voice_agent.store import SqliteStore
from voice_agent.wiring import lease_seconds, open_runtime


async def test_shared_store_startup_recovery_spares_other_tasks_live_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = fast_settings(tmp_path)
    seed = await SqliteStore.open(settings.db_path)
    await seed.begin_turn("c1", "t1", {"audio_b64": "AAAA"})  # another task's live turn
    await seed.close()
    monkeypatch.setattr(SqliteStore, "shared", True)  # behave like DynamoDB
    providers, *_ = fake_providers()
    runtime = await open_runtime(settings, providers=providers)
    try:
        assert runtime.recovered_dead_letters == []
        turn = await runtime.store.get_turn("c1", "t1")
        assert turn is not None and turn.status.value == "in_progress"
    finally:
        await runtime.close()


def test_lease_is_at_least_a_minute_and_three_deadlines(tmp_path: Path) -> None:
    assert lease_seconds(fast_settings(tmp_path, turn_deadline_s=6)) == 60
    assert lease_seconds(fast_settings(tmp_path, turn_deadline_s=30)) == 90


async def test_run_emf_flushes_active_calls_and_turn_metrics(tmp_path: Path) -> None:
    providers, *_ = fake_providers()
    settings = fast_settings(tmp_path, emf_enabled=True, emf_interval_s=0.01)
    runtime = await open_runtime(settings, providers=providers)
    try:
        assert runtime.emf is not None
        runtime.calls.touch("c1")
        await runtime.pipeline.handle_turn("c1", "t1", b"hello")
        docs: list[dict[str, Any]] = []
        flushed = asyncio.Event()

        def write(doc: dict[str, Any]) -> None:
            docs.append(doc)
            flushed.set()

        task = asyncio.create_task(runtime.run_emf(write))
        await asyncio.wait_for(flushed.wait(), timeout=5)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        first = next(d for d in docs if "Stage" not in d)
        assert first["ActiveCalls"] == 1 and first["Turns"] == 1
        assert first["DlqPending"] == 0 and len(first["TurnLatency"]) == 1
    finally:
        await runtime.close()


async def test_emf_is_off_by_default(tmp_path: Path) -> None:
    providers, *_ = fake_providers()
    runtime = await open_runtime(fast_settings(tmp_path), providers=providers)
    try:
        assert runtime.emf is None and runtime.metrics.emf is None
    finally:
        await runtime.close()


async def test_run_emf_still_flushes_when_the_dlq_count_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    providers, *_ = fake_providers()
    settings = fast_settings(tmp_path, emf_enabled=True, emf_interval_s=0.01)
    runtime = await open_runtime(settings, providers=providers)

    async def boom() -> dict[str, int]:
        raise RuntimeError("dynamodb down")

    monkeypatch.setattr(runtime.store, "dead_letter_counts", boom)
    try:
        runtime.calls.touch("c1")
        docs: list[dict[str, Any]] = []
        flushed = asyncio.Event()

        def write(doc: dict[str, Any]) -> None:
            docs.append(doc)
            flushed.set()

        task = asyncio.create_task(runtime.run_emf(write))
        await asyncio.wait_for(flushed.wait(), timeout=5)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        first = next(d for d in docs if "Stage" not in d)
        assert first["ActiveCalls"] == 1 and "DlqPending" not in first
    finally:
        await runtime.close()
