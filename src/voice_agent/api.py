"""HTTP API for the voice-agent service."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, field_validator

from voice_agent.config import Settings
from voice_agent.dlq import ReplayOutcome
from voice_agent.pipeline import TurnResult
from voice_agent.store import DEAD_LETTER_STATUSES, DeadLetter, Turn
from voice_agent.wiring import Runtime, open_runtime

_REPLAY_HTTP_STATUS = {
    "resolved": 200,
    "failed": 502,
    "skipped": 503,  # a circuit is open: retry after the cool-down
    "not_replayable": 409,
    "not_found": 404,
}


# Stores keep the request audio, and a DynamoDB item is capped at 400 KB: 300k base64
# characters is ~225 KB of audio (~7 s of 16 kHz/16-bit, ~14 s of 8 kHz telephony).
MAX_AUDIO_B64_CHARS = 300_000


class TurnRequest(BaseModel):
    turn_id: str = Field(default_factory=lambda: uuid.uuid4().hex, min_length=1, max_length=128)
    audio_b64: str = Field(
        min_length=1,
        max_length=MAX_AUDIO_B64_CHARS,
        description="Caller audio for this turn, base64",
    )

    @field_validator("audio_b64")
    @classmethod
    def _valid_base64(cls, value: str) -> str:
        try:
            base64.b64decode(value, validate=True)
        except binascii.Error as exc:
            raise ValueError("audio_b64 is not valid base64") from exc
        return value


def _turn_response(result: TurnResult) -> dict[str, Any]:
    return {
        "call_id": result.call_id,
        "turn_id": result.turn_id,
        "status": result.status.value,
        "action": result.action.value,
        "transcript": result.transcript,
        "reply_text": result.reply_text,
        "audio_b64": base64.b64encode(result.audio).decode("ascii") if result.audio else None,
        "dlq_id": result.dlq_id,
        "failed_stage": result.failed_stage,
        "error_kind": result.error_kind,
        "duration_ms": result.duration_ms,
        "duplicate": result.duplicate,
        "attempts": result.attempts,
    }


def _turn_record(turn: Turn) -> dict[str, Any]:
    return {
        "turn_id": turn.turn_id,
        "status": turn.status.value,
        "result": turn.result,
        "failed_stage": turn.failed_stage,
        "error_kind": turn.error_kind,
        "attempts": turn.attempts,
        "created_at": turn.created_at,
        "updated_at": turn.updated_at,
    }


def _dead_letter(entry: DeadLetter) -> dict[str, Any]:
    return {
        **entry.summary(),
        "error_detail": entry.error_detail,
        "payload": entry.payload,
        "attempts": entry.attempts,
        "partial": entry.partial,
        "last_replay_error": entry.last_replay_error,
        "replay_attempts": entry.replay_attempts,
        "last_replay_at": entry.last_replay_at,
        "resolved_at": entry.resolved_at,
    }


def _replay(outcome: ReplayOutcome) -> dict[str, Any]:
    return {
        "id": outcome.id,
        "status": outcome.status,
        "error": outcome.error,
        "error_kind": outcome.error_kind,
        "result": outcome.result,
        "attempts": outcome.attempts,
    }


def create_app(settings: Settings | None = None, *, runtime: Runtime | None = None) -> FastAPI:
    """Pass `runtime` to serve an already-wired runtime (tests); otherwise one is
    opened from `settings` at startup and closed at shutdown."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if runtime is not None:
            app.state.runtime = runtime
            yield
            return
        opened = await open_runtime(settings or Settings())
        app.state.runtime = opened
        tasks = [asyncio.create_task(opened.run_sweeper())]
        if opened.emf is not None:
            tasks.append(asyncio.create_task(opened.run_emf()))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await opened.close()

    app = FastAPI(
        title="Collections voice-agent",
        version="0.1.0",
        description="STT -> LLM -> TTS turns with retries, circuit breaking and a dead-letter "
        "queue.",
        lifespan=lifespan,
    )
    if runtime is not None:
        app.state.runtime = runtime  # ASGI test transports do not run the lifespan

    def rt(request: Request) -> Runtime:
        runtime_: Runtime = request.app.state.runtime
        return runtime_

    @app.post("/v1/calls/{call_id}/turns")
    async def post_turn(call_id: str, body: TurnRequest, request: Request) -> JSONResponse:
        audio = base64.b64decode(body.audio_b64)
        runtime_ = rt(request)
        with runtime_.calls.turn(call_id):
            result = await runtime_.pipeline.handle_turn(call_id, body.turn_id, audio)
        return JSONResponse(_turn_response(result), status_code=409 if result.duplicate else 200)

    @app.post("/v1/calls/{call_id}/end")
    async def end_call(call_id: str, request: Request) -> dict[str, Any]:
        return {"call_id": call_id, "was_active": rt(request).calls.end(call_id)}

    @app.get("/v1/calls/{call_id}")
    async def get_call(call_id: str, request: Request) -> dict[str, Any]:
        turns = await rt(request).store.list_turns(call_id)
        if not turns:
            raise HTTPException(404, f"no turns recorded for call {call_id!r}")
        return {"call_id": call_id, "turns": [_turn_record(t) for t in turns]}

    @app.get("/v1/dlq")
    async def list_dlq(
        request: Request,
        status: str | None = Query(None, pattern="^(" + "|".join(DEAD_LETTER_STATUSES) + ")$"),
        call_id: str | None = None,
        limit: int = Query(100, ge=1, le=1000),
    ) -> dict[str, Any]:
        store = rt(request).store
        items = await store.list_dead_letters(status=status, call_id=call_id, limit=limit)
        return {
            "counts": await store.dead_letter_counts(),
            "items": [d.summary() for d in items],
        }

    @app.get("/v1/dlq/{dlq_id}")
    async def get_dlq(dlq_id: int, request: Request) -> dict[str, Any]:
        entry = await rt(request).store.get_dead_letter(dlq_id)
        if entry is None:
            raise HTTPException(404, f"dead letter {dlq_id} not found")
        return _dead_letter(entry)

    @app.post("/v1/dlq/replay")
    async def replay_pending(
        request: Request, limit: int = Query(100, ge=1, le=1000)
    ) -> dict[str, Any]:
        outcomes = await rt(request).dlq.replay_pending(limit=limit)
        return {"results": [_replay(o) for o in outcomes]}

    @app.post("/v1/dlq/{dlq_id}/replay")
    async def replay_one(dlq_id: int, request: Request) -> JSONResponse:
        outcome = await rt(request).dlq.replay(dlq_id)
        return JSONResponse(_replay(outcome), status_code=_REPLAY_HTTP_STATUS[outcome.status])

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        # Liveness only. Deliberately independent of provider and store health: an
        # STT/LLM/TTS outage or a DynamoDB brownout must not make the load balancer
        # recycle orchestrator tasks that are degrading correctly. Dead-letter counts
        # are on GET /v1/dlq.
        runtime_ = rt(request)
        return {
            "status": "ok",
            "breakers": {name: s.breaker.snapshot() for name, s in runtime_.stages.items()},
        }

    @app.get("/metrics")
    async def metrics(request: Request) -> Response:
        runtime_ = rt(request)
        runtime_.metrics.active_calls.set(runtime_.calls.active())
        runtime_.metrics.in_flight_turns.set(runtime_.calls.in_flight)
        return Response(runtime_.metrics.render(), media_type="text/plain; version=0.0.4")

    return app
