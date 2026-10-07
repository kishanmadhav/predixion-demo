import asyncio

import pytest

from voice_agent.store import Store, TurnStatus

from .conftest import ATTEMPTS, degrade


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
        await store.degrade_turn(
            "nope",
            "t1",
            failed_stage="stt",
            error_kind="timeout",
            error_detail="x",
            attempts=[],
            partial={},
        )
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


# -- state-machine guards (a transition only applies from the expected state) ----------


async def test_degrade_refuses_a_turn_that_is_not_in_progress(store: Store) -> None:
    await store.begin_turn("c1", "t1", {})
    await store.complete_turn("c1", "t1", {}, [])
    with pytest.raises(LookupError):
        await store.degrade_turn(
            "c1",
            "t1",
            failed_stage="llm",
            error_kind="timeout",
            error_detail="x",
            attempts=[],
            partial={},
        )
    turn = await store.get_turn("c1", "t1")
    assert turn is not None and turn.status is TurnStatus.COMPLETED
    assert await store.list_dead_letters() == []
    # the failed transaction was rolled back cleanly: the connection is still usable
    assert await store.begin_turn("c1", "t2", {})


async def test_complete_only_applies_to_an_in_progress_turn(store: Store) -> None:
    await degrade(store)
    assert not await store.complete_turn("c1", "t1", {"x": 1}, [])
    turn = await store.get_turn("c1", "t1")
    assert turn is not None and turn.status is TurnStatus.DEGRADED


async def test_release_and_resolve_only_apply_to_a_claimed_entry(store: Store) -> None:
    dlq_id = await degrade(store)
    assert not await store.release_dead_letter(dlq_id, error="x", attempts=[])
    assert not await store.resolve_dead_letter(dlq_id, {}, [])
    dl = await store.get_dead_letter(dlq_id)
    assert dl is not None and dl.status == "pending" and dl.replay_count == 0


async def test_degrade_persists_the_action_given_to_the_caller(store: Store) -> None:
    await store.begin_turn("c1", "t1", {})
    await store.degrade_turn(
        "c1",
        "t1",
        failed_stage="llm",
        error_kind="timeout",
        error_detail="x",
        attempts=[],
        partial={},
        action="handoff",
    )
    turn = await store.get_turn("c1", "t1")
    assert turn is not None and turn.action == "handoff"


# -- leases: a running service reclaims stuck work without a restart ------------------


async def test_recover_with_lease_ignores_fresh_work(store: Store) -> None:
    await store.begin_turn("c1", "live", {})
    dlq_id = await degrade(store, "c2", "t1")
    await store.claim_dead_letter(dlq_id)
    assert await store.recover(stale_after_s=60) == []
    turn = await store.get_turn("c1", "live")
    assert turn is not None and turn.status is TurnStatus.IN_PROGRESS
    dl = await store.get_dead_letter(dlq_id)
    assert dl is not None and dl.status == "replaying"


async def test_recover_with_lease_reclaims_stale_work(store: Store) -> None:
    await store.begin_turn("c1", "stuck", {})
    dlq_id = await degrade(store, "c2", "t1")
    await store.claim_dead_letter(dlq_id)
    await asyncio.sleep(0.05)
    recovered = await store.recover(stale_after_s=0.01)
    assert len(recovered) == 1
    turn = await store.get_turn("c1", "stuck")
    assert turn is not None and turn.status is TurnStatus.INTERRUPTED
    dl = await store.get_dead_letter(dlq_id)
    assert dl is not None and dl.status == "pending"


async def test_history_before_returns_completed_turns_preceding_the_given_one(
    store: Store,
) -> None:
    for t in ("t1", "t2", "t3", "t4"):
        await store.begin_turn("c1", t, {"audio_b64": "x" * 1000})
    await store.complete_turn("c1", "t1", {"transcript": "a", "reply_text": "A"}, [])
    await store.degrade_turn(
        "c1",
        "t2",
        failed_stage="llm",
        error_kind="timeout",
        error_detail="x",
        attempts=[],
        partial={"transcript": "b"},
    )
    await store.complete_turn("c1", "t4", {"transcript": "d", "reply_text": "D"}, [])
    assert await store.history_before("c1", "t3", limit=5) == [("a", "A")]
    assert await store.history_before("c1", "t4", limit=5) == [("a", "A")]
    assert await store.history_before("c1", "t4", limit=0) == []


async def test_recover_without_lease_dead_letters_every_in_progress_turn(store: Store) -> None:
    await store.begin_turn("c1", "t1", {"audio_b64": "AAAA"})
    ids = await store.recover()
    assert len(ids) == 1
    turn = await store.get_turn("c1", "t1")
    assert turn is not None and turn.status is TurnStatus.INTERRUPTED
    entry = await store.get_dead_letter(ids[0])
    assert entry is not None and entry.reason == "interrupted" and entry.status == "pending"
