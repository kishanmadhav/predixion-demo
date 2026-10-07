"""Persistence: the per-turn audit log and the dead-letter queue, behind one contract."""

from __future__ import annotations

from typing import TYPE_CHECKING

from voice_agent.store.base import Store
from voice_agent.store.records import (
    DEAD_LETTER_STATUSES,
    LIVE_DEGRADED,
    DeadLetter,
    Turn,
    TurnStatus,
)
from voice_agent.store.sqlite import SqliteStore

if TYPE_CHECKING:
    from voice_agent.config import Settings


async def open_store(settings: Settings) -> Store:
    """The backend `settings.store_backend` names."""
    if settings.store_backend == "dynamodb":
        from voice_agent.store.dynamodb import DynamoStore  # boto3 only when needed

        return DynamoStore.open(
            settings.dynamodb_table,
            region=settings.aws_region,
            endpoint_url=settings.dynamodb_endpoint_url,
        )
    return await SqliteStore.open(settings.db_path)


__all__ = [
    "DEAD_LETTER_STATUSES",
    "LIVE_DEGRADED",
    "DeadLetter",
    "SqliteStore",
    "Store",
    "Turn",
    "TurnStatus",
    "open_store",
]
