from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.fakes import FakeLlm, FakeStt, FakeTts, fake_providers, fast_settings
from voice_agent.pipeline import Action
from voice_agent.store import TurnStatus
from voice_agent.wiring import Runtime, open_runtime


@pytest.fixture
async def rt(tmp_path: Path) -> AsyncIterator[tuple[Runtime, FakeStt, FakeLlm, FakeTts]]:
    providers, stt, llm, tts = fake_providers()
    runtime = await open_runtime(fast_settings(tmp_path), providers=providers)
    yield runtime, stt, llm, tts
    await runtime.close()


async def test_successful_turn_runs_all_stages_and_is_logged(
    rt: tuple[Runtime, FakeStt, FakeLlm, FakeTts],
) -> None:
    runtime, *_ = rt
    result = await runtime.pipeline.handle_turn("c1", "t1", b"hello")
    assert result.status is TurnStatus.COMPLETED
    assert result.action is Action.CONTINUE
    assert result.transcript == "caller said hello"
    assert result.reply_text == "reply to: caller said hello"
    assert result.audio == b"AUDIO<reply to: caller said hello>"
    assert result.dlq_id is None
    turn = await runtime.store.get_turn("c1", "t1")
    assert turn is not None and turn.status is TurnStatus.COMPLETED
    assert [a["stage"] for a in turn.attempts] == ["stt", "llm", "tts"]


async def test_transient_failure_is_retried_transparently(
    rt: tuple[Runtime, FakeStt, FakeLlm, FakeTts],
) -> None:
    runtime, _, llm, _ = rt
    llm.fail_next = 2
    result = await runtime.pipeline.handle_turn("c1", "t1", b"hi")
    assert result.status is TurnStatus.COMPLETED
    assert llm.calls == 3
    assert [a["outcome"] for a in result.attempts if a["stage"] == "llm"] == [
        "error",
        "error",
        "ok",
    ]


async def test_exhausted_stage_degrades_gracefully_and_dead_letters_the_turn(
    rt: tuple[Runtime, FakeStt, FakeLlm, FakeTts],
) -> None:
    runtime, _, llm, tts = rt
    llm.failing = True
    result = await runtime.pipeline.handle_turn("c1", "t1", b"I can pay friday")

    assert result.status is TurnStatus.DEGRADED
    assert result.action is Action.RETRY_PROMPT
    assert result.failed_stage == "llm"
    assert result.error_kind == "server_error"
    assert result.reply_text == runtime.pipeline.fallbacks[Action.RETRY_PROMPT].text
    assert result.audio == runtime.pipeline.fallbacks[Action.RETRY_PROMPT].audio
    assert tts.calls == 0  # fallback audio is pre-rendered: no dependency on TTS

    assert result.dlq_id is not None
    dl = await runtime.store.get_dead_letter(result.dlq_id)
    assert dl is not None
    assert dl.failed_stage == "llm"
    assert dl.partial["transcript"] == "caller said I can pay friday"
    assert len([a for a in dl.attempts if a["stage"] == "llm"]) == 3


async def test_repeated_degradation_hands_the_call_to_a_human(
    rt: tuple[Runtime, FakeStt, FakeLlm, FakeTts],
) -> None:
    runtime, stt, _, _ = rt
    stt.failing = True
    first = await runtime.pipeline.handle_turn("c1", "t1", b"a")
    second = await runtime.pipeline.handle_turn("c1", "t2", b"b")
    assert first.action is Action.RETRY_PROMPT
    assert second.action is Action.HANDOFF
    assert second.reply_text == runtime.pipeline.fallbacks[Action.HANDOFF].text
    other_call = await runtime.pipeline.handle_turn("c2", "t1", b"c")
    assert other_call.action is Action.RETRY_PROMPT  # counted per call


async def test_open_circuit_fails_fast_and_still_dead_letters(
    rt: tuple[Runtime, FakeStt, FakeLlm, FakeTts],
) -> None:
    runtime, _, _, tts = rt
    tts.failing = True
    for i in range(3):  # 3 turns x 3 attempts = 9 failures >= minimum_calls
        await runtime.pipeline.handle_turn("c1", f"t{i}", b"x")
    calls_before = tts.calls
    result = await runtime.pipeline.handle_turn("c2", "t1", b"x")
    assert tts.calls == calls_before  # breaker open: TTS not called at all
    assert result.error_kind == "circuit_open"
    assert result.dlq_id is not None


async def test_llm_receives_system_prompt_and_call_history(
    rt: tuple[Runtime, FakeStt, FakeLlm, FakeTts],
) -> None:
    runtime, _, llm, _ = rt
    await runtime.pipeline.handle_turn("c1", "t1", b"one")
    await runtime.pipeline.handle_turn("c1", "t2", b"two")
    messages = llm.seen[-1]
    assert messages[0].role == "system"
    assert [(m.role, m.content) for m in messages[1:]] == [
        ("user", "caller said one"),
        ("assistant", "reply to: caller said one"),
        ("user", "caller said two"),
    ]


async def test_duplicate_turn_is_not_reprocessed(
    rt: tuple[Runtime, FakeStt, FakeLlm, FakeTts],
) -> None:
    runtime, stt, _, _ = rt
    await runtime.pipeline.handle_turn("c1", "t1", b"one")
    duplicate = await runtime.pipeline.handle_turn("c1", "t1", b"one")
    assert duplicate.duplicate
    assert duplicate.status is TurnStatus.COMPLETED
    assert stt.calls == 1


async def test_unexpected_bug_still_leaves_a_dead_letter(
    rt: tuple[Runtime, FakeStt, FakeLlm, FakeTts], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, stt, _, _ = rt

    async def broken(audio: bytes) -> str:
        raise RuntimeError("adapter bug")

    monkeypatch.setattr(stt, "transcribe", broken)
    result = await runtime.pipeline.handle_turn("c1", "t1", b"x")
    assert result.status is TurnStatus.DEGRADED
    assert result.error_kind == "internal_error"
    assert result.dlq_id is not None
