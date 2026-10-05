"""SQLite persistence: the per-turn audit log and the dead-letter queue.

Two tables, one file, so a turn's status change and its dead-letter row are written
in a single transaction: a turn can never be marked degraded without a dead-letter
entry, or vice versa.

* `turns` is written ahead (status `in_progress`) before any provider is called,
  so a crash mid-turn leaves evidence that `recover()` turns into a dead letter.
* `dead_letters` holds everything needed to inspect and replay a failed turn.

Two rules keep the state machines honest:

* Every transition is guarded in SQL (`... WHERE status = <expected>`), so a stale
  or concurrent writer can never skip or reverse a state.
* Every write runs under the connection lock *and* shielded from cancellation, so a
  cancelled caller can never split a transaction or leave the connection mid-way.

The database is ordinary SQLite (WAL mode) and can be opened with any SQLite client.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar

import aiosqlite

T = TypeVar("T")

SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    call_id       TEXT NOT NULL,
    turn_id       TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN
                  ('in_progress', 'completed', 'degraded', 'interrupted', 'completed_on_replay')),
    action        TEXT,
    request_json  TEXT NOT NULL,
    result_json   TEXT,
    failed_stage  TEXT,
    error_kind    TEXT,
    attempts_json TEXT NOT NULL DEFAULT '[]',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (call_id, turn_id)
);
CREATE INDEX IF NOT EXISTS turns_status ON turns (status, updated_at);

CREATE TABLE IF NOT EXISTS dead_letters (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id             TEXT NOT NULL,
    turn_id             TEXT NOT NULL,
    reason              TEXT NOT NULL CHECK (reason IN ('stage_failed', 'interrupted')),
    failed_stage        TEXT,
    error_kind          TEXT,
    error_detail        TEXT,
    payload_json        TEXT NOT NULL,
    attempts_json       TEXT NOT NULL,
    partial_json        TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'replaying', 'resolved')),
    replay_count        INTEGER NOT NULL DEFAULT 0,
    last_replay_error   TEXT,
    replay_attempts_json TEXT,
    created_at          TEXT NOT NULL,
    claimed_at          TEXT,
    last_replay_at      TEXT,
    resolved_at         TEXT,
    UNIQUE (call_id, turn_id),
    FOREIGN KEY (call_id, turn_id) REFERENCES turns (call_id, turn_id)
);
CREATE INDEX IF NOT EXISTS dead_letters_status ON dead_letters (status);
"""

DEAD_LETTER_STATUSES = ("pending", "replaying", "resolved")


class TurnStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    DEGRADED = "degraded"
    INTERRUPTED = "interrupted"
    COMPLETED_ON_REPLAY = "completed_on_replay"


# Statuses meaning "the caller got a fallback instead of a real answer".
_LIVE_DEGRADED = (TurnStatus.DEGRADED, TurnStatus.INTERRUPTED, TurnStatus.COMPLETED_ON_REPLAY)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds")


def _now() -> str:
    return _iso(datetime.now(UTC))


def _dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _loads(value: str | None) -> Any:
    return None if value is None else json.loads(value)


@dataclass(frozen=True)
class Turn:
    call_id: str
    turn_id: str
    status: TurnStatus
    action: str | None
    request: dict[str, Any]
    result: dict[str, Any] | None
    failed_stage: str | None
    error_kind: str | None
    attempts: list[dict[str, Any]]
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> Turn:
        return cls(
            call_id=row["call_id"],
            turn_id=row["turn_id"],
            status=TurnStatus(row["status"]),
            action=row["action"],
            request=_loads(row["request_json"]),
            result=_loads(row["result_json"]),
            failed_stage=row["failed_stage"],
            error_kind=row["error_kind"],
            attempts=_loads(row["attempts_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass(frozen=True)
class DeadLetter:
    id: int
    call_id: str
    turn_id: str
    reason: str
    failed_stage: str | None
    error_kind: str | None
    error_detail: str | None
    payload: dict[str, Any]
    attempts: list[dict[str, Any]]
    partial: dict[str, Any]
    status: str
    replay_count: int
    last_replay_error: str | None
    replay_attempts: list[dict[str, Any]] | None
    created_at: str
    claimed_at: str | None
    last_replay_at: str | None
    resolved_at: str | None

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> DeadLetter:
        return cls(
            id=row["id"],
            call_id=row["call_id"],
            turn_id=row["turn_id"],
            reason=row["reason"],
            failed_stage=row["failed_stage"],
            error_kind=row["error_kind"],
            error_detail=row["error_detail"],
            payload=_loads(row["payload_json"]),
            attempts=_loads(row["attempts_json"]),
            partial=_loads(row["partial_json"]),
            status=row["status"],
            replay_count=row["replay_count"],
            last_replay_error=row["last_replay_error"],
            replay_attempts=_loads(row["replay_attempts_json"]),
            created_at=row["created_at"],
            claimed_at=row["claimed_at"],
            last_replay_at=row["last_replay_at"],
            resolved_at=row["resolved_at"],
        )

    def summary(self) -> dict[str, Any]:
        """Listing view: everything except the (possibly large) payload."""
        return {
            "id": self.id,
            "call_id": self.call_id,
            "turn_id": self.turn_id,
            "reason": self.reason,
            "failed_stage": self.failed_stage,
            "error_kind": self.error_kind,
            "status": self.status,
            "replay_count": self.replay_count,
            "created_at": self.created_at,
        }


class Store:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        # One connection is shared by all coroutines; the lock keeps one coroutine's
        # transaction from interleaving with another's statements.
        self._lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: str | Path) -> Store:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(path), isolation_level=None)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        # FULL: a committed dead letter survives power loss, not just a process crash.
        await conn.execute("PRAGMA synchronous=FULL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA busy_timeout=2000")  # the CLI may write concurrently
        await conn.executescript(SCHEMA)
        return cls(conn)

    async def close(self) -> None:
        await self._conn.close()

    # -- plumbing -------------------------------------------------------------

    async def _write(self, work: Callable[[], Awaitable[T]]) -> T:
        """Run `work` under the lock, shielded: once started, it runs to completion
        even if the caller is cancelled, so no transaction is ever cut in half."""

        async def locked() -> T:
            async with self._lock:
                return await work()

        return await asyncio.shield(locked())

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[None]:
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            if self._conn.in_transaction:
                await self._conn.execute("ROLLBACK")
            raise
        await self._conn.execute("COMMIT")

    async def _one(self, sql: str, params: tuple[Any, ...]) -> aiosqlite.Row | None:
        async with self._conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def _all(self, sql: str, params: tuple[Any, ...]) -> list[aiosqlite.Row]:
        async with self._conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def _read_one(self, sql: str, params: tuple[Any, ...]) -> aiosqlite.Row | None:
        async with self._lock:
            return await self._one(sql, params)

    async def _read_all(self, sql: str, params: tuple[Any, ...]) -> list[aiosqlite.Row]:
        async with self._lock:
            return await self._all(sql, params)

    # -- turns ----------------------------------------------------------------

    async def begin_turn(self, call_id: str, turn_id: str, request: dict[str, Any]) -> bool:
        """Write-ahead record of a turn. Returns False if the turn already exists."""

        async def work() -> bool:
            now = _now()
            cur = await self._conn.execute(
                "INSERT OR IGNORE INTO turns (call_id, turn_id, status, request_json,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (call_id, turn_id, TurnStatus.IN_PROGRESS, _dumps(request), now, now),
            )
            return cur.rowcount == 1

        return await self._write(work)

    async def get_turn(self, call_id: str, turn_id: str) -> Turn | None:
        row = await self._read_one(
            "SELECT * FROM turns WHERE call_id = ? AND turn_id = ?", (call_id, turn_id)
        )
        return Turn.from_row(row) if row else None

    async def list_turns(self, call_id: str) -> list[Turn]:
        rows = await self._read_all(
            "SELECT * FROM turns WHERE call_id = ? ORDER BY rowid", (call_id,)
        )
        return [Turn.from_row(r) for r in rows]

    async def history_before(
        self, call_id: str, turn_id: str, *, limit: int
    ) -> list[tuple[str, str]]:
        """(transcript, reply) of the call's completed turns that precede `turn_id`,
        oldest first: the conversation the caller actually heard."""
        if limit <= 0:
            return []
        rows = await self._read_all(
            "SELECT result_json FROM turns WHERE call_id = ? AND status = ? AND rowid <"
            " (SELECT rowid FROM turns WHERE call_id = ? AND turn_id = ?)"
            " ORDER BY rowid DESC LIMIT ?",
            (call_id, TurnStatus.COMPLETED, call_id, turn_id, limit),
        )
        history = []
        for row in reversed(rows):
            result = _loads(row["result_json"]) or {}
            history.append((str(result.get("transcript", "")), str(result.get("reply_text", ""))))
        return history

    async def complete_turn(
        self,
        call_id: str,
        turn_id: str,
        result: dict[str, Any],
        attempts: list[dict[str, Any]],
    ) -> bool:
        """Mark an in-progress turn completed. False if it was not in progress."""

        async def work() -> bool:
            cur = await self._conn.execute(
                "UPDATE turns SET status = ?, action = 'continue', result_json = ?,"
                " attempts_json = ?, updated_at = ?"
                " WHERE call_id = ? AND turn_id = ? AND status = ?",
                (
                    TurnStatus.COMPLETED,
                    _dumps(result),
                    _dumps(attempts),
                    _now(),
                    call_id,
                    turn_id,
                    TurnStatus.IN_PROGRESS,
                ),
            )
            return cur.rowcount == 1

        return await self._write(work)

    async def degrade_turn(
        self,
        call_id: str,
        turn_id: str,
        *,
        failed_stage: str,
        error_kind: str,
        error_detail: str,
        attempts: list[dict[str, Any]],
        partial: dict[str, Any],
        action: str | None = None,
    ) -> int:
        """Mark an in-progress turn degraded and dead-letter it, atomically.
        Returns the DLQ id; raises LookupError if the turn is not in progress."""

        async def work() -> int:
            async with self._transaction():
                return await self._dead_letter(
                    call_id,
                    turn_id,
                    status=TurnStatus.DEGRADED,
                    reason="stage_failed",
                    failed_stage=failed_stage,
                    error_kind=error_kind,
                    error_detail=error_detail,
                    attempts=attempts,
                    partial=partial,
                    action=action,
                )

        return await self._write(work)

    async def _dead_letter(
        self,
        call_id: str,
        turn_id: str,
        *,
        status: TurnStatus,
        reason: str,
        failed_stage: str | None,
        error_kind: str | None,
        error_detail: str | None,
        attempts: list[dict[str, Any]],
        partial: dict[str, Any],
        action: str | None,
    ) -> int:
        """Must run inside `_transaction()`."""
        now = _now()
        cur = await self._conn.execute(
            "UPDATE turns SET status = ?, action = ?, failed_stage = ?, error_kind = ?,"
            " attempts_json = ?, result_json = ?, updated_at = ?"
            " WHERE call_id = ? AND turn_id = ? AND status = ?",
            (
                status,
                action,
                failed_stage,
                error_kind,
                _dumps(attempts),
                _dumps(partial),
                now,
                call_id,
                turn_id,
                TurnStatus.IN_PROGRESS,
            ),
        )
        if cur.rowcount != 1:
            raise LookupError(f"turn {call_id}/{turn_id} is not in progress")
        cur = await self._conn.execute(
            "INSERT INTO dead_letters (call_id, turn_id, reason, failed_stage, error_kind,"
            " error_detail, payload_json, attempts_json, partial_json, created_at)"
            " SELECT call_id, turn_id, ?, ?, ?, ?, request_json, ?, ?, ? FROM turns"
            " WHERE call_id = ? AND turn_id = ?",
            (
                reason,
                failed_stage,
                error_kind,
                error_detail,
                _dumps(attempts),
                _dumps(partial),
                now,
                call_id,
                turn_id,
            ),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid

    async def consecutive_degraded(self, call_id: str) -> int:
        """How many of this call's most recent finished turns ended in a fallback."""
        rows = await self._read_all(
            "SELECT status FROM turns WHERE call_id = ? AND status != ?"
            " ORDER BY rowid DESC LIMIT 50",
            (call_id, TurnStatus.IN_PROGRESS),
        )
        count = 0
        for row in rows:
            if row["status"] not in _LIVE_DEGRADED:
                break
            count += 1
        return count

    async def recover(self, *, stale_after_s: float | None = None) -> list[int]:
        """Reclaim work that nobody is going to finish. Returns new dead-letter ids.

        * Turns still `in_progress` are marked `interrupted` and dead-lettered.
        * Dead letters still `replaying` go back to `pending`.

        With `stale_after_s=None` (service startup, before serving traffic) everything
        is reclaimed. With a value, only work untouched for that long is: this is the
        lease a running service's sweeper uses, so work orphaned by a cancelled request
        or a failed write is recovered without a restart. Only the service runs this;
        the CLI never does, so it cannot reclaim the service's in-flight work.
        """
        cutoff = (
            _iso(datetime.now(UTC) - timedelta(seconds=stale_after_s))
            if stale_after_s is not None
            else "9999"
        )
        detail = (
            "service stopped while the turn was in progress"
            if stale_after_s is None
            else f"turn did not finish within {stale_after_s:g}s"
        )

        async def work() -> list[int]:
            await self._conn.execute(
                "UPDATE dead_letters SET status = 'pending'"
                " WHERE status = 'replaying' AND COALESCE(claimed_at, '') < ?",
                (cutoff,),
            )
            rows = await self._all(
                "SELECT call_id, turn_id FROM turns WHERE status = ? AND updated_at < ?"
                " ORDER BY rowid",
                (TurnStatus.IN_PROGRESS, cutoff),
            )
            ids = []
            for row in rows:
                async with self._transaction():
                    ids.append(
                        await self._dead_letter(
                            row["call_id"],
                            row["turn_id"],
                            status=TurnStatus.INTERRUPTED,
                            reason="interrupted",
                            failed_stage=None,
                            error_kind=None,
                            error_detail=detail,
                            attempts=[],
                            partial={},
                            action=None,
                        )
                    )
            return ids

        return await self._write(work)

    # -- dead letters ---------------------------------------------------------

    async def get_dead_letter(self, dlq_id: int) -> DeadLetter | None:
        row = await self._read_one("SELECT * FROM dead_letters WHERE id = ?", (dlq_id,))
        return DeadLetter.from_row(row) if row else None

    async def list_dead_letters(
        self, *, status: str | None = None, call_id: str | None = None, limit: int = 100
    ) -> list[DeadLetter]:
        clauses, params = [], []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if call_id is not None:
            clauses.append("call_id = ?")
            params.append(call_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self._read_all(
            f"SELECT * FROM dead_letters {where} ORDER BY id LIMIT ?", (*params, limit)
        )
        return [DeadLetter.from_row(r) for r in rows]

    async def dead_letter_counts(self) -> dict[str, int]:
        rows = await self._read_all(
            "SELECT status, COUNT(*) AS n FROM dead_letters GROUP BY status", ()
        )
        counts = dict.fromkeys(DEAD_LETTER_STATUSES, 0)
        counts.update({r["status"]: r["n"] for r in rows})
        return counts

    async def claim_dead_letter(self, dlq_id: int) -> DeadLetter | None:
        """Atomically move a pending entry to `replaying`. None if not claimable."""

        async def work() -> DeadLetter | None:
            cur = await self._conn.execute(
                "UPDATE dead_letters SET status = 'replaying', claimed_at = ?"
                " WHERE id = ? AND status = 'pending'",
                (_now(), dlq_id),
            )
            if cur.rowcount != 1:
                return None
            row = await self._one("SELECT * FROM dead_letters WHERE id = ?", (dlq_id,))
            return DeadLetter.from_row(row) if row else None

        return await self._write(work)

    async def release_dead_letter(
        self, dlq_id: int, *, error: str, attempts: list[dict[str, Any]]
    ) -> bool:
        """A replay failed: back to pending, with the reason recorded."""

        async def work() -> bool:
            cur = await self._conn.execute(
                "UPDATE dead_letters SET status = 'pending', replay_count = replay_count + 1,"
                " last_replay_error = ?, replay_attempts_json = ?, last_replay_at = ?"
                " WHERE id = ? AND status = 'replaying'",
                (error, _dumps(attempts), _now(), dlq_id),
            )
            return cur.rowcount == 1

        return await self._write(work)

    async def resolve_dead_letter(
        self, dlq_id: int, result: dict[str, Any], attempts: list[dict[str, Any]]
    ) -> bool:
        """A replay succeeded: resolve the entry and complete the turn's record."""

        async def work() -> bool:
            now = _now()
            async with self._transaction():
                cur = await self._conn.execute(
                    "UPDATE dead_letters SET status = 'resolved', replay_count = replay_count + 1,"
                    " replay_attempts_json = ?, last_replay_at = ?, resolved_at = ?,"
                    " last_replay_error = NULL WHERE id = ? AND status = 'replaying'",
                    (_dumps(attempts), now, now, dlq_id),
                )
                if cur.rowcount != 1:
                    return False
                await self._conn.execute(
                    "UPDATE turns SET status = ?, result_json = ?, updated_at = ?"
                    " WHERE (call_id, turn_id) = (SELECT call_id, turn_id FROM dead_letters"
                    " WHERE id = ?)",
                    (TurnStatus.COMPLETED_ON_REPLAY, _dumps(result), now, dlq_id),
                )
                return True

        return await self._write(work)
