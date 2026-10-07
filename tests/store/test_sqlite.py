import contextlib
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from voice_agent.store import SqliteStore, TurnStatus

from .conftest import degrade


@pytest.fixture
async def sqlite_store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = await SqliteStore.open(tmp_path / "test.db")
    yield s
    await s.close()


async def test_recover_dead_letters_turns_interrupted_by_a_crash(tmp_path: Path) -> None:
    path = tmp_path / "crash.db"
    s = await SqliteStore.open(path)
    await s.begin_turn("c1", "t1", {"audio_b64": "AAAA"})  # process "dies" here
    dlq_id = await degrade(s, "c2", "t1")
    await s.claim_dead_letter(dlq_id)  # ... and mid-replay
    await s.close()

    s = await SqliteStore.open(path)
    recovered = await s.recover()
    assert len(recovered) == 1
    turn = await s.get_turn("c1", "t1")
    assert turn is not None and turn.status is TurnStatus.INTERRUPTED
    dl = await s.get_dead_letter(recovered[0])
    assert dl is not None and dl.reason == "interrupted" and dl.status == "pending"
    stuck = await s.get_dead_letter(dlq_id)
    assert stuck is not None and stuck.status == "pending"
    await s.close()


async def test_a_failed_commit_does_not_leave_the_connection_in_a_transaction(
    sqlite_store: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    await sqlite_store.begin_turn("c1", "t1", {})
    await sqlite_store.begin_turn("c1", "t2", {})
    conn = sqlite_store._conn
    real_execute = conn.execute
    failed_once = []

    def flaky_execute(sql: str, *args: Any) -> Any:
        if sql == "COMMIT" and not failed_once:
            failed_once.append(True)
            raise sqlite3.OperationalError("disk I/O error")
        return real_execute(sql, *args)

    monkeypatch.setattr(conn, "execute", flaky_execute)
    with pytest.raises(sqlite3.OperationalError):
        await sqlite_store.degrade_turn(
            "c1",
            "t1",
            failed_stage="llm",
            error_kind="timeout",
            error_detail="x",
            attempts=[],
            partial={},
        )
    assert not conn.in_transaction
    # the failed transaction was rolled back, and later writes work and persist
    assert await sqlite_store.degrade_turn(
        "c1",
        "t1",
        failed_stage="llm",
        error_kind="timeout",
        error_detail="x",
        attempts=[],
        partial={},
    )
    assert await sqlite_store.degrade_turn(
        "c1",
        "t2",
        failed_stage="llm",
        error_kind="timeout",
        error_detail="x",
        attempts=[],
        partial={},
    )


V1_SCHEMA = """
CREATE TABLE turns (call_id TEXT NOT NULL, turn_id TEXT NOT NULL, status TEXT NOT NULL,
    request_json TEXT NOT NULL, result_json TEXT, failed_stage TEXT, error_kind TEXT,
    attempts_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, PRIMARY KEY (call_id, turn_id));
CREATE TABLE dead_letters (id INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT NOT NULL,
    turn_id TEXT NOT NULL, reason TEXT NOT NULL, failed_stage TEXT, error_kind TEXT,
    error_detail TEXT, payload_json TEXT NOT NULL, attempts_json TEXT NOT NULL,
    partial_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
    replay_count INTEGER NOT NULL DEFAULT 0, last_replay_error TEXT,
    replay_attempts_json TEXT, created_at TEXT NOT NULL, last_replay_at TEXT,
    resolved_at TEXT, UNIQUE (call_id, turn_id));
INSERT INTO turns VALUES ('c1', 't1', 'in_progress', '{}', NULL, NULL, NULL, '[]',
    '2026-01-01T00:00:00.000+00:00', '2026-01-01T00:00:00.000+00:00');
"""


async def test_a_database_from_the_first_schema_version_is_migrated(tmp_path: Path) -> None:
    path = tmp_path / "v1.db"
    with contextlib.closing(sqlite3.connect(path)) as legacy:
        legacy.executescript(V1_SCHEMA)
        legacy.commit()
    store = await SqliteStore.open(path)
    try:
        assert len(await store.recover()) == 1  # uses claimed_at and action
        turn = await store.get_turn("c1", "t1")
        assert turn is not None and turn.status is TurnStatus.INTERRUPTED
    finally:
        await store.close()
