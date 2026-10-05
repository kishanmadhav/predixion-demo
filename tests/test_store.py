from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from voice_agent.store import Store, TurnStatus

ATTEMPTS = [{"stage": "llm", "attempt": 1, "outcome": "error", "error_kind": "server_error"}]


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    s = await Store.open(tmp_path / "test.db")
    yield s
    await s.close()


async def degrade(store: Store, call_id: str = "c1", turn_id: str = "t1") -> int:
    await store.begin_turn(call_id, turn_id, {"audio_b64": "AAAA"})
    return await store.degrade_turn(
        call_id,
        turn_id,
        failed_stage="llm",
        error_kind="server_error",
        error_detail="[llm] server_error: 503",
        attempts=ATTEMPTS,
        partial={"transcript": "I can pay next week"},
    )


async def test_begin_turn_writes_ahead_an_in_progress_row(store: Store) -> None:
    assert await store.begin_turn("c1", "t1", {"audio_b64": "AAAA"})
    turn = await store.get_turn("c1", "t1")
    assert turn is not None
    assert turn.status is TurnStatus.IN_PROGRESS
    assert turn.request == {"audio_b64": "AAAA"}


async def test_begin_turn_is_idempotent_per_call_and_turn(store: Store) -> None:
    assert await store.begin_turn("c1", "t1", {"audio_b64": "AAAA"})
    assert not await store.begin_turn("c1", "t1", {"audio_b64": "BBBB"})
    turn = await store.get_turn("c1", "t1")
    assert turn is not None and turn.request == {"audio_b64": "AAAA"}


async def test_complete_turn_records_result_and_attempts(store: Store) -> None:
    await store.begin_turn("c1", "t1", {"audio_b64": "AAAA"})
    await store.complete_turn("c1", "t1", {"transcript": "hi", "reply_text": "hello"}, ATTEMPTS)
    turn = await store.get_turn("c1", "t1")
    assert turn is not None
    assert turn.status is TurnStatus.COMPLETED
    assert turn.result == {"transcript": "hi", "reply_text": "hello"}
    assert turn.attempts == ATTEMPTS


async def test_degrade_turn_marks_turn_and_dead_letters_it_atomically(store: Store) -> None:
    dlq_id = await degrade(store)
    turn = await store.get_turn("c1", "t1")
    assert turn is not None and turn.status is TurnStatus.DEGRADED
    assert turn.failed_stage == "llm"
    dl = await store.get_dead_letter(dlq_id)
    assert dl is not None
    assert dl.status == "pending"
    assert dl.reason == "stage_failed"
    assert dl.payload == {"audio_b64": "AAAA"}
    assert dl.partial == {"transcript": "I can pay next week"}
    assert dl.attempts == ATTEMPTS
    assert dl.replay_count == 0


async def test_degrade_of_unknown_turn_raises_and_writes_nothing(store: Store) -> None:
    with pytest.raises(LookupError):
        await store.degrade_turn("nope", "t1", failed_stage="stt", error_kind="timeout",
                                 error_detail="x", attempts=[], partial={})
    assert await store.list_dead_letters() == []


async def test_claim_is_exclusive(store: Store) -> None:
    dlq_id = await degrade(store)
    first = await store.claim_dead_letter(dlq_id)
    second = await store.claim_dead_letter(dlq_id)
    assert first is not None and first.status == "replaying"
    assert second is None


async def test_release_returns_entry_to_pending_with_error(store: Store) -> None:
    dlq_id = await degrade(store)
    await store.claim_dead_letter(dlq_id)
    await store.release_dead_letter(dlq_id, error="[llm] circuit_open", attempts=ATTEMPTS)
    dl = await store.get_dead_letter(dlq_id)
    assert dl is not None
    assert dl.status == "pending"
    assert dl.replay_count == 1
    assert dl.last_replay_error == "[llm] circuit_open"
    assert dl.last_replay_at is not None


async def test_resolve_completes_turn_on_replay(store: Store) -> None:
    dlq_id = await degrade(store)
    await store.claim_dead_letter(dlq_id)
    await store.resolve_dead_letter(dlq_id, {"transcript": "x", "reply_text": "y"}, ATTEMPTS)
    dl = await store.get_dead_letter(dlq_id)
    assert dl is not None and dl.status == "resolved" and dl.resolved_at is not None
    turn = await store.get_turn("c1", "t1")
    assert turn is not None
    assert turn.status is TurnStatus.COMPLETED_ON_REPLAY
    assert turn.result == {"transcript": "x", "reply_text": "y"}
    assert await store.claim_dead_letter(dlq_id) is None


async def test_recover_dead_letters_turns_interrupted_by_a_crash(tmp_path: Path) -> None:
    path = tmp_path / "crash.db"
    s = await Store.open(path)
    await s.begin_turn("c1", "t1", {"audio_b64": "AAAA"})  # process "dies" here
    dlq_id = await degrade(s, "c2", "t1")
    await s.claim_dead_letter(dlq_id)  # ... and mid-replay
    await s.close()

    s = await Store.open(path)
    recovered = await s.recover()
    assert len(recovered) == 1
    turn = await s.get_turn("c1", "t1")
    assert turn is not None and turn.status is TurnStatus.INTERRUPTED
    dl = await s.get_dead_letter(recovered[0])
    assert dl is not None and dl.reason == "interrupted" and dl.status == "pending"
    stuck = await s.get_dead_letter(dlq_id)
    assert stuck is not None and stuck.status == "pending"
    await s.close()


async def test_consecutive_degraded_counts_trailing_non_completed_turns(store: Store) -> None:
    await store.begin_turn("c1", "t1", {})
    await store.complete_turn("c1", "t1", {}, [])
    assert await store.consecutive_degraded("c1") == 0
    await degrade(store, "c1", "t2")
    await degrade(store, "c1", "t3")
    assert await store.consecutive_degraded("c1") == 2
    await store.begin_turn("c1", "t4", {})
    await store.complete_turn("c1", "t4", {}, [])
    assert await store.consecutive_degraded("c1") == 0


async def test_list_and_count_dead_letters_by_status(store: Store) -> None:
    a = await degrade(store, "c1", "t1")
    await degrade(store, "c1", "t2")
    await store.claim_dead_letter(a)
    await store.resolve_dead_letter(a, {}, [])
    pending = await store.list_dead_letters(status="pending")
    assert [d.turn_id for d in pending] == ["t2"]
    assert await store.dead_letter_counts() == {"pending": 1, "replaying": 0, "resolved": 1}


async def test_list_turns_is_in_arrival_order(store: Store) -> None:
    for t in ["t1", "t2", "t3"]:
        await store.begin_turn("c1", t, {})
    assert [t.turn_id for t in await store.list_turns("c1")] == ["t1", "t2", "t3"]
