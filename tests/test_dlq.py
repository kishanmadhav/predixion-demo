import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

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
