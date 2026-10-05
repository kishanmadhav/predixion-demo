"""Dead-letter replay.

Replaying a turn re-runs its stored request through the *same* resilient pipeline
(same breakers, so a still-broken provider fails fast instead of being hammered).
The live moment has passed, so a successful replay completes the compliance record
(what the caller said, what the agent would have said) rather than speaking to the
caller; that record can drive a callback or follow-up.

Claiming is atomic (`pending -> replaying`), so two operators or workers can never
replay the same entry concurrently, and replays never create new dead letters.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from voice_agent.metrics import Metrics
from voice_agent.pipeline import TurnFailedError, TurnPipeline
from voice_agent.store import Store

log = logging.getLogger(__name__)

ReplayStatus = Literal["resolved", "failed", "not_replayable", "not_found"]


@dataclass(frozen=True)
class ReplayOutcome:
    id: int
    status: ReplayStatus
    error: str | None = None
    result: dict[str, Any] | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)


class DeadLetterService:
    def __init__(
        self,
        *,
        store: Store,
        pipeline: TurnPipeline,
        metrics: Metrics,
        turn_deadline_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._pipeline = pipeline
        self._metrics = metrics
        self._deadline_s = turn_deadline_s
        self._clock = clock

    async def replay(self, dlq_id: int) -> ReplayOutcome:
        entry = await self._store.claim_dead_letter(dlq_id)
        if entry is None:
            existing = await self._store.get_dead_letter(dlq_id)
            if existing is None:
                return ReplayOutcome(dlq_id, "not_found")
            return ReplayOutcome(dlq_id, "not_replayable", error=f"status is {existing.status!r}")

        try:
            audio = base64.b64decode(entry.payload["audio_b64"], validate=True)
        except (KeyError, binascii.Error) as exc:
            error = f"stored payload is unusable: {exc!r}"
            await self._store.release_dead_letter(dlq_id, error=error, attempts=[])
            return self._done(ReplayOutcome(dlq_id, "failed", error=error))

        try:
            processed = await self._pipeline.process(
                entry.call_id, entry.turn_id, audio, deadline=self._clock() + self._deadline_s
            )
        except TurnFailedError as failure:
            await self._store.release_dead_letter(
                dlq_id, error=failure.detail, attempts=failure.attempts
            )
            return self._done(
                ReplayOutcome(dlq_id, "failed", error=failure.detail, attempts=failure.attempts)
            )
        except BaseException as exc:
            # Never leave an entry stuck in `replaying`.
            await asyncio.shield(
                self._store.release_dead_letter(dlq_id, error=repr(exc), attempts=[])
            )
            raise

        await self._store.resolve_dead_letter(dlq_id, processed.record(), processed.attempts)
        return self._done(
            ReplayOutcome(dlq_id, "resolved", result=processed.record(),
                          attempts=processed.attempts)
        )

    async def replay_pending(self, *, limit: int = 100) -> list[ReplayOutcome]:
        entries = await self._store.list_dead_letters(status="pending", limit=limit)
        return [await self.replay(entry.id) for entry in entries]

    def _done(self, outcome: ReplayOutcome) -> ReplayOutcome:
        self._metrics.replays.labels(outcome.status).inc()
        log.info("dlq replay id=%s status=%s error=%s", outcome.id, outcome.status, outcome.error)
        return outcome
