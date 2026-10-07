from pathlib import Path

import pytest

from tests.fakes import fake_providers, fast_settings
from voice_agent.store import SqliteStore
from voice_agent.wiring import lease_seconds, open_runtime


async def test_shared_store_startup_recovery_spares_other_tasks_live_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = fast_settings(tmp_path)
    seed = await SqliteStore.open(settings.db_path)
    await seed.begin_turn("c1", "t1", {"audio_b64": "AAAA"})  # another task's live turn
    await seed.close()
    monkeypatch.setattr(SqliteStore, "shared", True)  # behave like DynamoDB
    providers, *_ = fake_providers()
    runtime = await open_runtime(settings, providers=providers)
    try:
        assert runtime.recovered_dead_letters == []
        turn = await runtime.store.get_turn("c1", "t1")
        assert turn is not None and turn.status.value == "in_progress"
    finally:
        await runtime.close()


def test_lease_is_at_least_a_minute_and_three_deadlines(tmp_path: Path) -> None:
    assert lease_seconds(fast_settings(tmp_path, turn_deadline_s=6)) == 60
    assert lease_seconds(fast_settings(tmp_path, turn_deadline_s=30)) == 90
