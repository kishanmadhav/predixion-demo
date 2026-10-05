import base64
import io
import wave
from collections.abc import AsyncIterator

import httpx
import pytest

from mock_provider.app import MockConfig, create_app


def make_client(**overrides: object) -> httpx.AsyncClient:
    params: dict[str, object] = {
        "failure_rate": 0.0,
        "seed": 1,
        "latency_scale": 0.0,
        "hang_s": 0.01,
    }
    params.update(overrides)
    app = create_app(MockConfig(**params))  # type: ignore[arg-type]
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mock")


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with make_client() as c:
        yield c


async def call_all(client: httpx.AsyncClient) -> list[httpx.Response]:
    return [
        await client.post("/v1/stt", json={"audio_b64": base64.b64encode(b"x").decode()}),
        await client.post("/v1/llm", json={"messages": [{"role": "user", "content": "hi"}]}),
        await client.post("/v1/tts", json={"text": "Hello there"}),
    ]


async def test_healthy_mock_serves_all_three_stages(client: httpx.AsyncClient) -> None:
    stt, llm, tts = await call_all(client)
    assert stt.status_code == llm.status_code == tts.status_code == 200
    assert isinstance(stt.json()["text"], str) and stt.json()["text"]
    assert isinstance(llm.json()["text"], str) and llm.json()["text"]
    audio = base64.b64decode(tts.json()["audio_b64"])
    with wave.open(io.BytesIO(audio)) as wav:
        assert wav.getnframes() > 0


async def test_failure_rate_one_fails_every_request() -> None:
    async with make_client(failure_rate=1.0) as client:
        for _ in range(10):
            for response in await call_all(client):
                assert response.status_code != 200 or "text" not in _safe_json(response)
        stats = (await client.get("/admin/stats")).json()
    assert stats["stages"]["llm"]["requests"] == 10
    assert stats["stages"]["llm"]["ok"] == 0


def _safe_json(response: httpx.Response) -> dict[str, object]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


async def test_default_failure_rate_is_roughly_twenty_percent() -> None:
    async with make_client(failure_rate=0.2, seed=42) as client:
        for _ in range(500):
            await client.post("/v1/llm", json={"messages": []})
        llm = (await client.get("/admin/stats")).json()["stages"]["llm"]
    assert llm["requests"] == 500
    assert 0.14 <= 1 - llm["ok"] / 500 <= 0.26


async def test_failures_use_a_mix_of_modes() -> None:
    async with make_client(failure_rate=1.0, seed=3) as client:
        for _ in range(200):
            await client.post("/v1/llm", json={"messages": []})
        llm = (await client.get("/admin/stats")).json()["stages"]["llm"]
    for mode in ("http_503", "http_500", "http_429", "hang", "malformed"):
        assert llm[mode] > 0, mode


async def test_rate_limit_failures_send_retry_after() -> None:
    async with make_client(failure_rate=1.0, seed=3) as client:
        for _ in range(100):
            r = await client.post("/v1/llm", json={"messages": []})
            if r.status_code == 429:
                assert float(r.headers["Retry-After"]) > 0
                return
    pytest.fail("no 429 produced")


async def test_chaos_outage_fails_one_stage_then_recovers(client: httpx.AsyncClient) -> None:
    r = await client.post("/admin/chaos", json={"stage": "llm", "outage_s": 60})
    assert r.status_code == 200
    stt, llm, tts = await call_all(client)
    assert stt.status_code == 200 and tts.status_code == 200
    assert llm.status_code == 503
    await client.post("/admin/chaos", json={"stage": "llm", "outage_s": 0})
    assert (await call_all(client))[1].status_code == 200


async def test_chaos_sets_failure_rate_for_all_stages(client: httpx.AsyncClient) -> None:
    await client.post("/admin/chaos", json={"failure_rate": 1.0})
    stats = (await client.get("/admin/stats")).json()
    assert stats["failure_rates"] == {"stt": 1.0, "llm": 1.0, "tts": 1.0}


async def test_chaos_rejects_bad_input(client: httpx.AsyncClient) -> None:
    assert (await client.post("/admin/chaos", json={"failure_rate": 2})).status_code == 422
    assert (await client.post("/admin/chaos", json={"stage": "asr"})).status_code == 422


async def test_reset_clears_stats_and_chaos(client: httpx.AsyncClient) -> None:
    await client.post("/admin/chaos", json={"failure_rate": 1.0, "stage": "tts"})
    await call_all(client)
    await client.post("/admin/reset")
    stats = (await client.get("/admin/stats")).json()
    assert stats["stages"]["tts"]["requests"] == 0
    assert stats["failure_rates"]["tts"] == 0.0


async def test_same_seed_gives_same_outcomes() -> None:
    async def outcomes() -> list[int]:
        async with make_client(failure_rate=0.5, seed=9) as client:
            return [
                (await client.post("/v1/llm", json={"messages": []})).status_code for _ in range(30)
            ]

    assert await outcomes() == await outcomes()
