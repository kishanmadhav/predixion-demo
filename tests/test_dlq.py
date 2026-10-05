import asyncio
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from tests.fakes import FakeLlm, FakeStt, FakeTts, fake_providers, fast_settings
from voice_agent.store import TurnStatus
from voice_agent.wiring import Runtime, open_runtime


@pytest.fixture
async def rt(tmp_path: Path) -> AsyncIterator[tuple[Runtime, FakeStt, FakeLlm, FakeTts]]:
    providers, stt, llm, tts = fake_providers()
    runtime = await open_runtime(fast_settings(tmp_path), providers=providers)
    yield runtime, stt, llm, tts
    await runtime.close()


async def test_replay_after_recovery_resolves_the_entry(
    rt: tuple[Runtime, FakeStt, FakeLlm, FakeTts],
) -> None:
    runtime, _, llm, _ = rt
    llm.failing = True
    failed = await runtime.pipeline.handle_turn("c1", "t1", b"pay friday")
    assert failed.dlq_id is not None
    llm.failing = False

    outcome = await runtime.dlq.replay(failed.dlq_id)

    assert outcome.status == "resolved"
    dl = await runtime.store.get_dead_letter(failed.dlq_id)
    assert dl is not None and dl.status == "resolved" and dl.replay_count == 1
    turn = await runtime.store.get_turn("c1", "t1")
    assert turn is not None
    assert turn.status is TurnStatus.COMPLETED_ON_REPLAY
    assert turn.result is not None
    assert turn.result["reply_text"] == "reply to: caller said pay friday"


async def test_replay_while_provider_still_down_returns_entry_to_pending(
    rt: tuple[Runtime, FakeStt, FakeLlm, FakeTts],
) -> None:
    runtime, _, llm, _ = rt
    llm.failing = True
    failed = await runtime.pipeline.handle_turn("c1", "t1", b"x")
    assert failed.dlq_id is not None

    outcome = await runtime.dlq.replay(failed.dlq_id)

    assert outcome.status == "failed"
    assert outcome.error is not None and "llm" in outcome.error
    dl = await runtime.store.get_dead_letter(failed.dlq_id)
    assert dl is not None
    assert dl.status == "pending"
    assert dl.replay_count == 1
    assert dl.replay_attempts  # the replay's own attempts are kept for inspection
    assert len(await runtime.store.list_dead_letters()) == 1  # replay never adds rows


async def test_resolved_entry_cannot_be_replayed_again(
    rt: tuple[Runtime, FakeStt, FakeLlm, FakeTts],
) -> None:
    runtime, stt, _, _ = rt
    stt.fail_next = 3
    failed = await runtime.pipeline.handle_turn("c1", "t1", b"x")
    assert failed.dlq_id is not None
    assert (await runtime.dlq.replay(failed.dlq_id)).status == "resolved"
    calls = stt.calls
    again = await runtime.dlq.replay(failed.dlq_id)
    assert again.status == "not_replayable"
    assert stt.calls == calls


async def test_replay_of_unknown_id_is_not_found(
    rt: tuple[Runtime, FakeStt, FakeLlm, FakeTts],
) -> None:
    runtime, *_ = rt
    assert (await runtime.dlq.replay(999)).status == "not_found"


async def test_replay_uses_history_from_before_the_failed_turn(
    rt: tuple[Runtime, FakeStt, FakeLlm, FakeTts],
) -> None:
    runtime, _, llm, _ = rt
    await runtime.pipeline.handle_turn("c1", "t1", b"one")
    llm.fail_next = 3
    failed = await runtime.pipeline.handle_turn("c1", "t2", b"two")
    await runtime.pipeline.handle_turn("c1", "t3", b"three")
    assert failed.dlq_id is not None
    await asyncio.sleep(0.25)  # 3 of the last 5 LLM calls failed: breaker is cooling down

    assert (await runtime.dlq.replay(failed.dlq_id)).status == "resolved"

    contents = [m.content for m in llm.seen[-1][1:]]
    assert contents == ["caller said one", "reply to: caller said one", "caller said two"]


async def test_replay_pending_processes_every_pending_entry(
    rt: tuple[Runtime, FakeStt, FakeLlm, FakeTts],
) -> None:
    runtime, stt, _, _ = rt
    stt.failing = True
    for i in range(3):
        await runtime.pipeline.handle_turn(f"c{i}", "t1", b"x")
    stt.failing = False
    await asyncio.sleep(0.25)  # the STT breaker opened; let it cool down to half-open

    outcomes = await runtime.dlq.replay_pending(limit=10)

    assert [o.status for o in outcomes] == ["resolved"] * 3
    assert (await runtime.store.dead_letter_counts())["pending"] == 0


async def test_replay_pending_runs_replays_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    providers, _, llm, _ = fake_providers()
    # A breaker that will not trip, so this test measures concurrency only.
    settings = fast_settings(tmp_path, breaker_window=100, breaker_minimum_calls=100)
    runtime = await open_runtime(settings, providers=providers)
    for i in range(6):
        llm.fail_next = 3
        await runtime.pipeline.handle_turn(f"c{i}", "t1", b"x")

    real_transcribe = runtime.providers.stt.transcribe

    async def slow_transcribe(audio: bytes) -> str:
        await asyncio.sleep(0.2)
        return await real_transcribe(audio)

    monkeypatch.setattr(runtime.providers.stt, "transcribe", slow_transcribe)
    loop = asyncio.get_running_loop()
    started = loop.time()
    outcomes = await runtime.dlq.replay_pending(limit=10, concurrency=6)
    elapsed = loop.time() - started
    await runtime.close()

    assert [o.status for o in outcomes] == ["resolved"] * 6
    assert elapsed < 1.0  # sequential would take >= 1.2s


async def test_bulk_replay_stops_early_while_the_provider_is_still_down(
    rt: tuple[Runtime, FakeStt, FakeLlm, FakeTts],
) -> None:
    runtime, _, llm, _ = rt
    llm.failing = True
    for i in range(4):
        await runtime.pipeline.handle_turn(f"c{i}", "t1", b"x")

    assert runtime.pipeline.open_circuits() == ["llm"]

    outcomes = await runtime.dlq.replay_pending(limit=10)

    # Open circuit: nothing is claimed or attempted, nothing is charged a replay.
    assert [o.status for o in outcomes] == ["skipped"] * 4
    entries = await runtime.store.list_dead_letters(status="pending")
    assert [e.replay_count for e in entries] == [0, 0, 0, 0]

    await asyncio.sleep(0.25)  # half-open now, but the provider is still failing
    outcomes = await runtime.dlq.replay_pending(limit=10)

    assert [o.status for o in outcomes] == ["failed", "skipped", "skipped", "skipped"]
    entries = await runtime.store.list_dead_letters(status="pending")
    assert [e.replay_count for e in entries] == [1, 0, 0, 0]  # skipped ones untouched


async def test_single_replay_is_deferred_while_a_circuit_is_open(
    rt: tuple[Runtime, FakeStt, FakeLlm, FakeTts],
) -> None:
    runtime, _, llm, _ = rt
    llm.failing = True
    results = [await runtime.pipeline.handle_turn(f"c{i}", "t1", b"x") for i in range(3)]
    dlq_id = results[0].dlq_id
    assert dlq_id is not None
    calls = llm.calls

    outcome = await runtime.dlq.replay(dlq_id)

    assert outcome.status == "skipped"
    assert outcome.error is not None and "llm" in outcome.error
    assert llm.calls == calls
    dl = await runtime.store.get_dead_letter(dlq_id)
    assert dl is not None and dl.status == "pending" and dl.replay_count == 0


async def test_ramp_up_is_not_stopped_by_an_unusable_payload(
    rt: tuple[Runtime, FakeStt, FakeLlm, FakeTts],
) -> None:
    runtime, _, llm, _ = rt
    await runtime.store.begin_turn("bad", "t1", {"audio_b64": "!!not base64!!"})
    await runtime.store.degrade_turn(
        "bad",
        "t1",
        failed_stage="stt",
        error_kind="timeout",
        error_detail="x",
        attempts=[],
        partial={},
    )
    llm.failing = True
    for i in range(3):
        await runtime.pipeline.handle_turn(f"c{i}", "t1", b"x")
    llm.failing = False
    await asyncio.sleep(0.25)  # LLM breaker half-open: replay ramps up one at a time

    outcomes = await runtime.dlq.replay_pending(limit=10)

    assert [o.status for o in outcomes] == ["failed", "resolved", "resolved", "resolved"]
    assert outcomes[0].error_kind == "bad_payload"


async def test_cancellation_during_resolve_does_not_strand_the_entry(
    rt: tuple[Runtime, FakeStt, FakeLlm, FakeTts], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _, llm, _ = rt
    llm.fail_next = 3
    failed = await runtime.pipeline.handle_turn("c1", "t1", b"x")
    assert failed.dlq_id is not None
    real = runtime.store.resolve_dead_letter

    async def slow_resolve(*args: Any, **kwargs: Any) -> bool:
        await asyncio.sleep(0.2)
        return await real(*args, **kwargs)

    monkeypatch.setattr(runtime.store, "resolve_dead_letter", slow_resolve)
    task = asyncio.create_task(runtime.dlq.replay(failed.dlq_id))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.3)
    dl = await runtime.store.get_dead_letter(failed.dlq_id)
    assert dl is not None and dl.status == "resolved"


async def test_failed_resolve_write_releases_the_entry(
    rt: tuple[Runtime, FakeStt, FakeLlm, FakeTts], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _, llm, _ = rt
    llm.fail_next = 3
    failed = await runtime.pipeline.handle_turn("c1", "t1", b"x")
    assert failed.dlq_id is not None

    async def broken_resolve(*args: Any, **kwargs: Any) -> bool:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(runtime.store, "resolve_dead_letter", broken_resolve)
    outcome = await runtime.dlq.replay(failed.dlq_id)

    assert outcome.status == "failed"
    dl = await runtime.store.get_dead_letter(failed.dlq_id)
    assert dl is not None and dl.status == "pending" and dl.replay_count == 1


async def test_concurrent_replays_of_the_same_entry_run_it_once(
    rt: tuple[Runtime, FakeStt, FakeLlm, FakeTts],
) -> None:
    runtime, _, llm, _ = rt
    llm.fail_next = 3
    failed = await runtime.pipeline.handle_turn("c1", "t1", b"x")
    assert failed.dlq_id is not None
    outcomes = await asyncio.gather(*(runtime.dlq.replay(failed.dlq_id) for _ in range(3)))
    assert sorted(o.status for o in outcomes) == ["not_replayable", "not_replayable", "resolved"]


async def test_interrupted_turns_are_replayable(tmp_path: Path) -> None:
    providers, *_ = fake_providers()
    settings = fast_settings(tmp_path)
    runtime = await open_runtime(settings, providers=providers)
    await runtime.store.begin_turn("c1", "t1", {"audio_b64": "aGk="})  # crash mid-turn
    await runtime.close()

    runtime = await open_runtime(settings, providers=providers)  # startup recovery
    pending = await runtime.store.list_dead_letters(status="pending")
    assert [d.reason for d in pending] == ["interrupted"]
    outcome = await runtime.dlq.replay(pending[0].id)
    assert outcome.status == "resolved"
    await runtime.close()


async def test_running_sweeper_reclaims_orphaned_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Runtime, "lease_s", property(lambda self: 0.01))
    providers, *_ = fake_providers()
    runtime = await open_runtime(
        fast_settings(tmp_path, sweep_interval_s=0.05), providers=providers
    )
    await runtime.store.begin_turn("c1", "orphan", {"audio_b64": "aGk="})  # never finished
    sweeper = asyncio.create_task(runtime.run_sweeper())
    await asyncio.sleep(0.3)
    sweeper.cancel()
    pending = await runtime.store.list_dead_letters(status="pending")
    assert [(d.turn_id, d.reason) for d in pending] == [("orphan", "interrupted")]
    await runtime.close()


async def test_bulk_replay_from_a_fresh_process_sends_one_canary_first(tmp_path: Path) -> None:
    """A CLI process starts with closed breakers that know nothing about an ongoing
    outage; it must not fan out a batch of replays into a dead provider."""
    providers, _, llm, _ = fake_providers()
    settings = fast_settings(tmp_path, breaker_minimum_calls=10, breaker_window=10)
    service = await open_runtime(settings, providers=providers)
    llm.failing = True
    for i in range(3):
        await service.pipeline.handle_turn(f"c{i}", "t1", b"x")
    await service.close()

    cli = await open_runtime(settings, providers=providers, recover=False)
    assert cli.pipeline.circuits_closed()
    calls = llm.calls
    outcomes = await cli.dlq.replay_pending(limit=10, concurrency=8)
    await cli.close()

    assert [o.status for o in outcomes] == ["failed", "skipped", "skipped"]
    assert llm.calls - calls == settings.retry_max_attempts  # just the canary's attempts


async def test_canary_must_resolve_before_replays_fan_out(tmp_path: Path) -> None:
    providers, _, llm, _ = fake_providers()
    settings = fast_settings(tmp_path, breaker_minimum_calls=10, breaker_window=10)
    service = await open_runtime(settings, providers=providers)
    await service.store.begin_turn("bad", "t1", {"audio_b64": "!!not base64!!"})
    await service.store.degrade_turn(
        "bad",
        "t1",
        failed_stage="stt",
        error_kind="timeout",
        error_detail="x",
        attempts=[],
        partial={},
    )
    llm.failing = True
    for i in range(5):
        await service.pipeline.handle_turn(f"c{i}", "t1", b"x")
    await service.close()

    cli = await open_runtime(settings, providers=providers, recover=False)
    calls = llm.calls
    outcomes = await cli.dlq.replay_pending(limit=10, concurrency=8)
    await cli.close()

    # the bad payload proves nothing about provider health, so the next replay is
    # still a lone canary; it fails, and the rest are skipped untouched
    assert [o.status for o in outcomes] == ["failed", "failed"] + ["skipped"] * 4
    assert llm.calls - calls == settings.retry_max_attempts


async def test_replay_reports_status_before_circuit_state(
    rt: tuple[Runtime, FakeStt, FakeLlm, FakeTts],
) -> None:
    runtime, _, llm, _ = rt
    llm.fail_next = 3
    first = await runtime.pipeline.handle_turn("c0", "t1", b"x")
    assert first.dlq_id is not None
    assert (await runtime.dlq.replay(first.dlq_id)).status == "resolved"
    llm.failing = True
    for i in range(3):
        await runtime.pipeline.handle_turn(f"c{i + 1}", "t1", b"x")
    assert runtime.pipeline.open_circuits() == ["llm"]
    assert (await runtime.dlq.replay(first.dlq_id)).status == "not_replayable"
