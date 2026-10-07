"""Chaos proxy: the Round 1 failure mix in front of real OpenAI-compatible servers.

Each request independently fails with the configured probability, using exactly the
mock provider's failure modes (503/500/429/hang/malformed); healthy requests are
forwarded untouched to vLLM (chat) or Speaches (audio). The service talks to this proxy
with its `openai` adapters, so the swap from mock to real models is configuration only.
"""

from __future__ import annotations

import asyncio
import os
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from mock_provider.chaos import ChaosState, MockConfig, Stage, add_admin_routes

ROUTES: dict[str, tuple[Stage, str]] = {
    "/v1/chat/completions": ("llm", "llm"),
    "/v1/audio/transcriptions": ("stt", "audio"),
    "/v1/audio/speech": ("tts", "audio"),
}


def _malformed(stage: Stage) -> Response:
    """A fresh "malformed 200" per request; each breaks the adapter's parser."""
    if stage == "llm":
        return Response('{"choices": [{"message": {"cont', media_type="application/json")
    if stage == "stt":
        return Response('{"result": null}', media_type="application/json")
    return Response(b"", media_type="audio/wav")


@dataclass
class ProxyConfig:
    chaos: MockConfig
    llm_upstream: str
    audio_upstream: str
    timeout_s: float = 60.0

    @classmethod
    def from_env(cls) -> ProxyConfig:
        return cls(
            chaos=MockConfig.from_env(),
            llm_upstream=os.environ.get("PROXY_LLM_UPSTREAM", "http://127.0.0.1:8000"),
            audio_upstream=os.environ.get("PROXY_AUDIO_UPSTREAM", "http://127.0.0.1:8001"),
            timeout_s=float(os.environ.get("PROXY_TIMEOUT_S", "60")),
        )


def create_proxy_app(config: ProxyConfig, *, client: httpx.AsyncClient | None = None) -> FastAPI:
    state = ChaosState(
        config=config.chaos,
        rng=random.Random(config.chaos.seed),
        failure_rates=dict.fromkeys(("stt", "llm", "tts"), config.chaos.failure_rate),
    )
    owns_client = client is None
    http = client or httpx.AsyncClient(
        timeout=httpx.Timeout(config.timeout_s, connect=5.0),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if owns_client:
            await http.aclose()

    upstreams = {"llm": config.llm_upstream.rstrip("/"), "audio": config.audio_upstream.rstrip("/")}
    app = FastAPI(title="Chaos proxy", version="0.1.0", lifespan=lifespan)
    add_admin_routes(app, state)

    async def forward(path: str, request: Request) -> Response:
        stage, upstream = ROUTES[path]
        stats = state.stats[stage]
        stats["requests"] += 1
        outcome = state.pick_outcome(stage)
        if outcome != "ok":
            stats[outcome] += 1
            return await injected(stage, outcome)
        body = await request.body()
        headers = {"content-type": request.headers.get("content-type", "application/json")}
        try:
            upstream_response = await http.post(
                upstreams[upstream] + path, content=body, headers=headers
            )
        except httpx.HTTPError as exc:
            stats["upstream_error"] += 1
            return JSONResponse(
                {"error": f"upstream unreachable: {type(exc).__name__}"}, status_code=502
            )
        stats["ok" if upstream_response.status_code < 400 else "upstream_error"] += 1
        return Response(
            upstream_response.content,
            status_code=upstream_response.status_code,
            media_type=upstream_response.headers.get("content-type"),
        )

    async def injected(stage: Stage, outcome: str) -> Response:
        if outcome == "outage":
            return JSONResponse({"error": "service unavailable (outage)"}, status_code=503)
        if outcome == "hang":
            await asyncio.sleep(config.chaos.hang_s)
            return JSONResponse({"error": "upstream timeout"}, status_code=504)
        if outcome == "http_503":
            return JSONResponse({"error": "model server overloaded"}, status_code=503)
        if outcome == "http_500":
            return JSONResponse({"error": "internal error"}, status_code=500)
        if outcome == "http_429":
            retry_after = f"{state.rng.uniform(0.1, 0.5):.2f}"
            return JSONResponse(
                {"error": "rate limited"}, status_code=429, headers={"Retry-After": retry_after}
            )
        return _malformed(stage)

    for route in ROUTES:

        async def handler(request: Request, _path: str = route) -> Response:
            return await forward(_path, request)

        app.add_api_route(route, handler, methods=["POST"])

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> Response:
        results: dict[str, Any] = {}
        for name, base in upstreams.items():
            try:
                results[name] = (await http.get(base + "/health", timeout=3)).status_code
            except httpx.HTTPError as exc:
                results[name] = type(exc).__name__
        ok = all(code == 200 for code in results.values())
        return JSONResponse({"ready": ok, "upstreams": results}, status_code=200 if ok else 503)

    return app


def app() -> FastAPI:  # uvicorn factory for containers
    return create_proxy_app(ProxyConfig.from_env())
