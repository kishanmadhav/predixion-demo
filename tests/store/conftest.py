from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from voice_agent.store import SqliteStore, Store

ATTEMPTS = [{"stage": "llm", "attempt": 1, "outcome": "error", "error_kind": "server_error"}]


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    s = await SqliteStore.open(tmp_path / "test.db")
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
