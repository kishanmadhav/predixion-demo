"""The storage contract. Both backends (SQLite locally, DynamoDB on AWS) pass the same
contract tests in tests/store/test_contract.py."""

from __future__ import annotations

from typing import Any, Protocol

from voice_agent.store.records import DeadLetter, Turn, TurnStatus


class Store(Protocol):
    # True when several service instances share the store (DynamoDB). Startup recovery
    # must then only reclaim stale work, never another live instance's in-flight turns.
    shared: bool

    async def close(self) -> None: ...
    async def begin_turn(self, call_id: str, turn_id: str, request: dict[str, Any]) -> bool: ...
    async def get_turn(self, call_id: str, turn_id: str) -> Turn | None: ...
    # Arrival order. A backend may leave `request` (the audio) empty; `get_turn` has it.
    async def list_turns(self, call_id: str) -> list[Turn]: ...
    async def history_before(
        self, call_id: str, turn_id: str, *, limit: int
    ) -> list[tuple[str, str]]: ...
    async def complete_turn(
        self, call_id: str, turn_id: str, result: dict[str, Any], attempts: list[dict[str, Any]]
    ) -> bool: ...
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
    ) -> int: ...
    async def consecutive_degraded(self, call_id: str) -> int: ...
    async def recover(self, *, stale_after_s: float | None = None) -> list[int]: ...
    async def get_dead_letter(self, dlq_id: int) -> DeadLetter | None: ...
    # Listing records: only the `summary()` fields are guaranteed. A backend may leave
    # payload, attempts, partial and the replay history empty (DynamoDB serves listings
    # from an index without the audio); `get_dead_letter` returns the full entry.
    async def list_dead_letters(
        self, *, status: str | None = None, call_id: str | None = None, limit: int = 100
    ) -> list[DeadLetter]: ...
    async def dead_letter_counts(self) -> dict[str, int]: ...
    async def claim_dead_letter(self, dlq_id: int) -> DeadLetter | None: ...
    async def release_dead_letter(
        self, dlq_id: int, *, error: str, attempts: list[dict[str, Any]]
    ) -> bool: ...
    async def resolve_dead_letter(
        self, dlq_id: int, result: dict[str, Any], attempts: list[dict[str, Any]]
    ) -> bool: ...


__all__ = ["Store", "TurnStatus"]
