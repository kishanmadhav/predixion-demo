import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from mock_provider.chaos import MockConfig
from mock_provider.proxy import ProxyConfig, create_proxy_app
from voice_agent.providers.base import ChatMessage, ProviderError
from voice_agent.providers.openai_compat import OpenAICompatLlm, OpenAICompatStt, OpenAICompatTts


def upstream() -> FastAPI:
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def chat(request: Request) -> JSONResponse:
        body = await request.json()
        return JSONResponse({"choices": [{"message": {"content": f"echo:{body['model']}"}}]})

    @app.post("/v1/audio/transcriptions")
    async def stt(request: Request) -> JSONResponse:
        async with request.form() as form:
            return JSONResponse({"text": f"heard {form['model']}"})

    @app.post("/v1/audio/speech")
    async def tts() -> Response:
        return Response(b"RIFF....WAVE", media_type="audio/wav")

    @app.post("/v1/limited")
    async def limited() -> Response:
        return Response(status_code=429, headers={"Retry-After": "7"})

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def proxy(rate: float) -> httpx.AsyncClient:
    up = httpx.AsyncClient(transport=httpx.ASGITransport(app=upstream()), base_url="http://up")
    config = ProxyConfig(
        chaos=MockConfig(failure_rate=rate, seed=1, hang_s=0.05),
        llm_upstream="http://up",
        audio_upstream="http://up",
        timeout_s=5,
    )
    app = create_proxy_app(config, client=up)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy")


async def test_healthy_requests_reach_the_real_servers_through_the_openai_adapters() -> None:
    async with proxy(0.0) as c:
        assert (
            await OpenAICompatLlm(c, "http://proxy/v1", model="m").complete(
                [ChatMessage("user", "hi")]
            )
            == "echo:m"
        )
        assert (
            await OpenAICompatStt(c, "http://proxy/v1", model="w").transcribe(b"RIFF") == "heard w"
        )
        assert (
            await OpenAICompatTts(c, "http://proxy/v1", model="k", voice="v").synthesize("hi")
            == b"RIFF....WAVE"
        )
        stats = (await c.get("/admin/stats")).json()["stages"]
        assert stats["llm"]["ok"] == 1 and stats["stt"]["ok"] == 1 and stats["tts"]["ok"] == 1
        assert stats["llm"]["upstream_error"] == 0


@pytest.mark.parametrize("stage", ["llm", "stt", "tts"])
async def test_injected_failures_surface_as_retryable_provider_errors(stage: str) -> None:
    async with proxy(1.0) as c:
        adapters = {
            "llm": lambda: OpenAICompatLlm(c, "http://proxy/v1", model="m").complete(
                [ChatMessage("user", "hi")]
            ),
            "stt": lambda: OpenAICompatStt(c, "http://proxy/v1", model="w").transcribe(b"RIFF"),
            "tts": lambda: OpenAICompatTts(c, "http://proxy/v1", model="k", voice="v").synthesize(
                "hi"
            ),
        }
        kinds = set()
        for _ in range(40):
            with pytest.raises(ProviderError) as err:
                await adapters[stage]()
            assert err.value.retryable
            kinds.add(err.value.kind)
        assert len(kinds) >= 2  # a mix of server_error / rate_limited / malformed


async def test_outage_switch_fails_every_request_for_that_stage_only() -> None:
    async with proxy(0.0) as c:
        await c.post("/admin/chaos", json={"stage": "llm", "outage_s": 30})
        r = await c.post("/v1/chat/completions", json={"model": "m", "messages": []})
        assert r.status_code == 503
        r = await c.post("/v1/audio/speech", json={"model": "k", "input": "x", "voice": "v"})
        assert r.status_code == 200


async def test_ready_reflects_upstream_health() -> None:
    async with proxy(0.0) as c:
        assert (await c.get("/ready")).status_code == 200


async def test_query_parameter_cannot_reroute_a_request() -> None:
    async with proxy(0.0) as c:
        r = await c.post("/v1/audio/speech?_path=/v1/chat/completions")
        assert r.headers["content-type"] == "audio/wav"


async def test_upstream_retry_after_is_passed_through() -> None:
    up = httpx.AsyncClient(transport=httpx.ASGITransport(app=upstream()), base_url="http://up")
    config = ProxyConfig(
        chaos=MockConfig(failure_rate=0.0, seed=1, hang_s=0.05),
        llm_upstream="http://up",
        audio_upstream="http://up",
        timeout_s=5,
    )
    app = create_proxy_app(config, client=up)
    # Route the upstream's 429 through the same forwarder by pointing a stage at /v1/limited.
    from mock_provider import proxy as proxy_module

    proxy_module.ROUTES["/v1/limited"] = ("llm", "llm")
    try:
        app = create_proxy_app(config, client=up)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://proxy"
        ) as c:
            r = await c.post("/v1/limited")
    finally:
        del proxy_module.ROUTES["/v1/limited"]
    assert r.status_code == 429
    assert r.headers["retry-after"] == "7"
