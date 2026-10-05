import asyncio
import json
from pathlib import Path

import pytest

from voice_agent.cli import main
from voice_agent.store import Store


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "cli.db"
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.chdir(tmp_path)  # keep any developer .env out of the test

    async def seed() -> None:
        store = await Store.open(path)
        await store.begin_turn("call-1", "t1", {"audio_b64": "aGk="})
        await store.degrade_turn(
            "call-1",
            "t1",
            failed_stage="llm",
            error_kind="circuit_open",
            error_detail="[llm] circuit_open",
            attempts=[],
            partial={"transcript": "hi"},
        )
        await store.close()

    asyncio.run(seed())
    return path


def test_dlq_list_prints_a_table(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["dlq", "list"]) == 0
    out = capsys.readouterr().out
    assert "pending=1" in out
    assert "circuit_open" in out
    assert "call-1/t1" in out


def test_dlq_list_json(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["dlq", "list", "--json", "--status", "pending"]) == 0
    entries = json.loads(capsys.readouterr().out)
    assert entries[0]["failed_stage"] == "llm"


def test_dlq_show_includes_partial_results(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["dlq", "show", "1"]) == 0
    detail = json.loads(capsys.readouterr().out)
    assert detail["partial"] == {"transcript": "hi"}


def test_dlq_show_unknown_id_fails(db: Path) -> None:
    assert main(["dlq", "show", "42"]) == 1


def test_dlq_replay_requires_a_target(db: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["dlq", "replay"])
    assert exc.value.code == 2
