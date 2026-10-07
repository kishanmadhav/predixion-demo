import asyncio
import json
from pathlib import Path

import pytest

from voice_agent.cli import build_parser, main
from voice_agent.store import SqliteStore


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "cli.db"
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.chdir(tmp_path)  # keep any developer .env out of the test

    async def seed() -> None:
        store = await SqliteStore.open(path)
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


def test_cli_replay_never_reclaims_the_services_in_flight_turns(
    db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.fakes import fake_providers

    async def live_turn() -> None:
        store = await SqliteStore.open(db)
        await store.begin_turn("live-call", "t1", {"audio_b64": "aGk="})  # service is mid-turn
        await store.close()

    asyncio.run(live_turn())
    providers, *_ = fake_providers()
    monkeypatch.setattr("voice_agent.wiring.build_providers", lambda settings, client: providers)

    assert main(["dlq", "replay", "--all"]) == 0
    assert "resolved" in capsys.readouterr().out

    async def check() -> None:
        store = await SqliteStore.open(db)
        turn = await store.get_turn("live-call", "t1")
        assert turn is not None and turn.status == "in_progress"  # left alone
        assert [d.status for d in await store.list_dead_letters()] == ["resolved"]
        await store.close()

    asyncio.run(check())


def test_chaos_proxy_command_defaults() -> None:
    args = build_parser().parse_args(["chaos-proxy"])
    assert args.host == "0.0.0.0"
    assert args.port == 8080
