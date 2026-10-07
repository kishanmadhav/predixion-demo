"""Mock STT / LLM / TTS provider having a bad day.

Each request independently fails with probability `failure_rate` (default 0.2),
using a realistic mix of failure modes:

    503 Service Unavailable   40%   overloaded / restarting
    500 Internal Server Error 20%
    429 Too Many Requests     15%   with a Retry-After header
    hang                      15%   no response for `hang_s` (then 504) - exercises timeouts
    malformed 200             10%   truncated JSON or wrong shape

Latency is lognormal per stage (STT ~150 ms, LLM ~400 ms, TTS ~200 ms median).
Chaos controls (`POST /admin/chaos`) change failure rates or force a hard outage
of one stage at runtime; `GET /admin/stats` reports what the provider actually
received, so retries and circuit-breaker fail-fast are visible from downstream.
"""

from __future__ import annotations

import asyncio
import base64
import io
import random
import wave
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from mock_provider.chaos import STAGES, ChaosState, MockConfig, Stage, add_admin_routes

TRANSCRIPTS = (
    "I can pay half now and the rest next Friday.",
    "Who is this calling?",
    "I already paid this last week.",
    "Can I speak to a real person?",
    "I lost my job, I need more time.",
    "Okay, what is the balance?",
)


class SttRequest(BaseModel):
    audio_b64: str


class LlmRequest(BaseModel):
    messages: list[dict[str, Any]]


class TtsRequest(BaseModel):
    text: str


def _silent_wav(text: str) -> bytes:
    """8 kHz mono 16-bit silence, ~60 ms per word: stands in for synthesized speech."""
    frames = 8000 * 60 // 1000 * max(1, len(text.split()))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\x00\x00" * frames)
    return buffer.getvalue()


def _reply(messages: list[dict[str, Any]]) -> str:
    last = next(
        (str(m.get("content", "")) for m in reversed(messages) if m.get("role") == "user"), ""
    ).lower()
    if "person" in last or "human" in last:
        return "Of course. I'm connecting you to a member of our team now."
    if "paid" in last:
        return "Thanks for letting me know. I'll check that payment and confirm by text today."
    if "pay" in last or "time" in last:
        return "I understand. I can set up a payment plan that works for you."
    return "Thanks. I'm calling about your account; is now a good time to talk?"


def create_app(config: MockConfig | None = None) -> FastAPI:
    config = config or MockConfig.from_env()
    state = ChaosState(
        config=config,
        rng=random.Random(config.seed),
        failure_rates=dict.fromkeys(STAGES, config.failure_rate),
    )
    app = FastAPI(title="Mock STT/LLM/TTS provider", version="0.1.0")

    async def serve(stage: Stage, body: dict[str, Any]) -> Response:
        stats = state.stats[stage]
        stats["requests"] += 1
        outcome = state.pick_outcome(stage)
        stats[outcome] += 1
        if outcome == "outage":
            return JSONResponse({"error": "service unavailable (outage)"}, status_code=503)
        if outcome == "hang":
            await asyncio.sleep(config.hang_s)
            return JSONResponse({"error": "upstream timeout"}, status_code=504)
        await asyncio.sleep(state.latency(stage))
        if outcome == "http_503":
            return JSONResponse({"error": "model server overloaded"}, status_code=503)
        if outcome == "http_500":
            return JSONResponse({"error": "internal error"}, status_code=500)
        if outcome == "http_429":
            retry_after = f"{state.rng.uniform(0.1, 0.5):.2f}"
            return JSONResponse(
                {"error": "rate limited"}, status_code=429, headers={"Retry-After": retry_after}
            )
        if outcome == "malformed":
            if state.rng.random() < 0.5:
                return Response('{"text": "I can pa', media_type="application/json")
            return JSONResponse({"result": None})
        return JSONResponse(body)

    @app.post("/v1/stt")
    async def stt(req: SttRequest) -> Response:
        index = sum(req.audio_b64.encode()) % len(TRANSCRIPTS)
        return await serve("stt", {"text": TRANSCRIPTS[index]})

    @app.post("/v1/llm")
    async def llm(req: LlmRequest) -> Response:
        return await serve("llm", {"text": _reply(req.messages)})

    @app.post("/v1/tts")
    async def tts(req: TtsRequest) -> Response:
        audio = base64.b64encode(_silent_wav(req.text)).decode("ascii")
        return await serve("tts", {"audio_b64": audio})

    add_admin_routes(app, state)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
