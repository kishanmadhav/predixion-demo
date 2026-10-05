import base64
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from tests.fakes import FakeLlm, FakeStt, FakeTts, fake_providers, fast_settings
from voice_agent.api import create_app
from voice_agent.wiring import open_runtime

AUDIO = base64.b64encode(b"hello").decode()

Ctx = tuple[httpx.AsyncClient, FakeStt, FakeLlm, FakeTts]


@pytest.fixture
async def ctx(tmp_path: Path) -> AsyncIterator[Ctx]:
    providers, stt, llm, tts = fake_providers()
    runtime = await open_runtime(fast_settings(tmp_path), providers=providers)
    app = create_app(runtime=runtime)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://va") as client:
        yield client, stt, llm, tts
    await runtime.close()


async def test_turn_endpoint_returns_reply_audio(ctx: Ctx) -> None:
    client, *_ = ctx
    r = await client.post("/v1/calls/c1/turns", json={"turn_id": "t1", "audio_b64": AUDIO})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed"
    assert body["action"] == "continue"
    assert base64.b64decode(body["audio_b64"]) == b"AUDIO<reply to: caller said hello>"
    assert body["dlq_id"] is None


async def test_turn_id_is_generated_when_omitted(ctx: Ctx) -> None:
    client, *_ = ctx
    r = await client.post("/v1/calls/c1/turns", json={"audio_b64": AUDIO})
    assert r.status_code == 200
    assert r.json()["turn_id"]


async def test_degraded_turn_is_still_a_200_with_fallback_and_dlq_id(ctx: Ctx) -> None:
    client, _, llm, _ = ctx
    llm.failing = True
    r = await client.post("/v1/calls/c1/turns", json={"turn_id": "t1", "audio_b64": AUDIO})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "degraded"
    assert body["action"] == "retry_prompt"
    assert body["failed_stage"] == "llm"
    assert body["audio_b64"]
    dl = (await client.get(f"/v1/dlq/{body['dlq_id']}")).json()
    assert dl["failed_stage"] == "llm"
    assert dl["payload"] == {"audio_b64": AUDIO}


async def test_duplicate_turn_returns_409_with_original_outcome(ctx: Ctx) -> None:
    client, stt, _, _ = ctx
    await client.post("/v1/calls/c1/turns", json={"turn_id": "t1", "audio_b64": AUDIO})
    r = await client.post("/v1/calls/c1/turns", json={"turn_id": "t1", "audio_b64": AUDIO})
    assert r.status_code == 409
    assert r.json()["status"] == "completed"
    assert r.json()["duplicate"] is True
    assert stt.calls == 1


async def test_invalid_audio_is_rejected(ctx: Ctx) -> None:
    client, *_ = ctx
    r = await client.post("/v1/calls/c1/turns", json={"audio_b64": "***"})
    assert r.status_code == 422


async def test_call_audit_trail_lists_every_turn(ctx: Ctx) -> None:
    client, _, llm, _ = ctx
    await client.post("/v1/calls/c1/turns", json={"turn_id": "t1", "audio_b64": AUDIO})
    llm.failing = True
    await client.post("/v1/calls/c1/turns", json={"turn_id": "t2", "audio_b64": AUDIO})
    turns = (await client.get("/v1/calls/c1")).json()["turns"]
    assert [(t["turn_id"], t["status"]) for t in turns] == [("t1", "completed"),
                                                           ("t2", "degraded")]


async def test_unknown_call_is_404(ctx: Ctx) -> None:
    client, *_ = ctx
    assert (await client.get("/v1/calls/nope")).status_code == 404


async def test_dlq_list_filter_and_replay(ctx: Ctx) -> None:
    client, _, llm, _ = ctx
    llm.failing = True
    r = await client.post("/v1/calls/c1/turns", json={"turn_id": "t1", "audio_b64": AUDIO})
    dlq_id = r.json()["dlq_id"]
    listing = (await client.get("/v1/dlq", params={"status": "pending"})).json()
    assert [d["id"] for d in listing["items"]] == [dlq_id]
    assert "payload" not in listing["items"][0]  # listings stay small
    assert listing["counts"]["pending"] == 1

    llm.failing = False
    replay = await client.post(f"/v1/dlq/{dlq_id}/replay")
    assert replay.status_code == 200
    assert replay.json()["status"] == "resolved"
    again = await client.post(f"/v1/dlq/{dlq_id}/replay")
    assert again.status_code == 409


async def test_failed_replay_reports_502(ctx: Ctx) -> None:
    client, _, llm, _ = ctx
    llm.failing = True
    r = await client.post("/v1/calls/c1/turns", json={"turn_id": "t1", "audio_b64": AUDIO})
    replay = await client.post(f"/v1/dlq/{r.json()['dlq_id']}/replay")
    assert replay.status_code == 502
    assert replay.json()["status"] == "failed"


async def test_replay_unknown_dlq_entry_is_404(ctx: Ctx) -> None:
    client, *_ = ctx
    assert (await client.post("/v1/dlq/123/replay")).status_code == 404
    assert (await client.get("/v1/dlq/123")).status_code == 404


async def test_bulk_replay(ctx: Ctx) -> None:
    client, stt, _, _ = ctx
    stt.fail_next = 3
    await client.post("/v1/calls/c1/turns", json={"turn_id": "t1", "audio_b64": AUDIO})
    r = await client.post("/v1/dlq/replay", params={"limit": 10})
    assert [o["status"] for o in r.json()["results"]] == ["resolved"]


async def test_health_stays_up_while_a_provider_is_down(ctx: Ctx) -> None:
    client, _, _, tts = ctx
    tts.failing = True
    for i in range(3):
        await client.post("/v1/calls/c1/turns", json={"turn_id": f"t{i}", "audio_b64": AUDIO})
    r = await client.get("/health")
    assert r.status_code == 200  # never let a provider outage kill healthy service tasks
    body = r.json()
    assert body["breakers"]["tts"]["state"] == "open"
    assert body["breakers"]["stt"]["state"] == "closed"
    assert body["dead_letters"]["pending"] == 3


async def test_metrics_expose_retries_breaker_state_and_dead_letters(ctx: Ctx) -> None:
    client, _, llm, _ = ctx
    llm.fail_next = 1
    await client.post("/v1/calls/c1/turns", json={"turn_id": "t1", "audio_b64": AUDIO})
    text = (await client.get("/metrics")).text
    assert 'provider_retries_total{stage="llm"} 1.0' in text
    assert 'circuit_breaker_state{stage="llm"} 0.0' in text
    assert 'turns_total{status="completed"} 1.0' in text
