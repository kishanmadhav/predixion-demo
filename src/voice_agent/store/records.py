"""Records shared by every store backend: turn and dead-letter shapes, statuses, helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

DEAD_LETTER_STATUSES = ("pending", "replaying", "resolved")


class TurnStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    DEGRADED = "degraded"
    INTERRUPTED = "interrupted"
    COMPLETED_ON_REPLAY = "completed_on_replay"


# Statuses meaning "the caller got a fallback instead of a real answer".
LIVE_DEGRADED = (TurnStatus.DEGRADED, TurnStatus.INTERRUPTED, TurnStatus.COMPLETED_ON_REPLAY)


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds")


def now() -> str:
    return iso(datetime.now(UTC))


def dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def loads(value: str | None) -> Any:
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
