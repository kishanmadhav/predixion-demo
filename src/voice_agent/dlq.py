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
from voice_agent.store import DeadLetter, Store

log = logging.getLogger(__name__)

ReplayStatus = Literal["resolved", "failed", "not_replayable", "not_found", "skipped"]

# Replay failures that are about one entry, not about the providers or configuration.
# Any other failure stops a bulk replay: it would affect every entry the same way.
ENTRY_SPECIFIC_KINDS = frozenset({"bad_payload"})


@dataclass(frozen=True)
class ReplayOutcome:
    id: int
    status: ReplayStatus
    error: str | None = None
    error_kind: str | None = None
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
        existing = await self._store.get_dead_letter(dlq_id)
        if existing is None:
            return ReplayOutcome(dlq_id, "not_found")
        if existing.status != "pending":
            return ReplayOutcome(dlq_id, "not_replayable", error=f"status is {existing.status!r}")
        blocked = self._pipeline.open_circuits()
        if blocked:
            # Don't claim (or charge a replay to) an entry that would fail fast anyway.
            return self._done(
                ReplayOutcome(
                    dlq_id,
                    "skipped",
                    error=f"circuit open for {', '.join(blocked)}; retry after the cool-down",
                    error_kind="circuit_open",
                )
            )

        entry = await self._store.claim_dead_letter(dlq_id)
        if entry is None:  # another worker claimed it between our read and our claim
            return ReplayOutcome(dlq_id, "not_replayable", error="claimed by another replay")
        # Shielded: once claimed, the entry always ends resolved or released, even if
        # the request that started the replay is cancelled.
        return await asyncio.shield(self._replay_claimed(entry))

    async def _replay_claimed(self, entry: DeadLetter) -> ReplayOutcome:
        dlq_id = entry.id
        try:
            audio = base64.b64decode(entry.payload["audio_b64"], validate=True)
        except (KeyError, TypeError, binascii.Error) as exc:
            return await self._failed(dlq_id, f"stored payload is unusable: {exc!r}", "bad_payload")

        try:
            processed = await self._pipeline.process(
                entry.call_id, entry.turn_id, audio, deadline=self._clock() + self._deadline_s
            )
        except TurnFailedError as failure:
            return await self._failed(dlq_id, failure.detail, failure.error_kind, failure.attempts)
        except Exception as exc:
            log.exception("unexpected error replaying dead letter %s", dlq_id)
            return await self._failed(dlq_id, repr(exc), "internal_error")

        try:
            recorded = await self._store.resolve_dead_letter(
                dlq_id, processed.record(), processed.attempts
            )
        except Exception as exc:
            log.exception("could not record replay result for dead letter %s", dlq_id)
            return await self._failed(
                dlq_id,
                f"could not record replay result: {exc!r}",
                "internal_error",
                processed.attempts,
            )
        if not recorded:
            return self._done(
                ReplayOutcome(dlq_id, "not_replayable", error="claim was lost before resolve")
            )
        return self._done(
            ReplayOutcome(
                dlq_id, "resolved", result=processed.record(), attempts=processed.attempts
            )
        )

    async def _failed(
        self,
        dlq_id: int,
        error: str,
        error_kind: str,
        attempts: list[dict[str, Any]] | None = None,
    ) -> ReplayOutcome:
        try:
            await self._store.release_dead_letter(dlq_id, error=error, attempts=attempts or [])
        except Exception:
            # The claim lease expires and the service's sweeper returns it to pending.
            log.exception("could not release dead letter %s", dlq_id)
        return self._done(
            ReplayOutcome(
                dlq_id, "failed", error=error, error_kind=error_kind, attempts=attempts or []
            )
        )

    async def replay_pending(
        self, *, limit: int = 100, concurrency: int = 8
    ) -> list[ReplayOutcome]:
        """Replay up to `limit` pending entries, oldest first, respecting backpressure.

        Entries are replayed one at a time until one resolves (a canary) and while any
        circuit is not fully closed (e.g. half-open right after an outage): a half-open
        breaker admits only a few probes, and flooding it would just bounce entries back.
        If a replay is deferred or fails for any reason other than its own bad payload,
        the rest are `skipped` (left pending, untouched).
        Once every circuit is closed, the remainder runs `concurrency` at a time.
        Claims are atomic, so overlapping workers cannot double-replay an entry.
        Results keep DLQ order.
        """
        ids = [e.id for e in await self._store.list_dead_letters(status="pending", limit=limit)]
        results: list[ReplayOutcome] = []

        # One at a time until a replay has resolved *and* every circuit is closed: a
        # process whose breakers have seen no traffic yet (the CLI) cannot tell an
        # ongoing outage from health, so the first successful replay is the proof.
        proven = False
        while ids and not (proven and self._pipeline.circuits_closed()):
            outcome = await self.replay(ids.pop(0))
            results.append(outcome)
            proven = proven or outcome.status == "resolved"
            unhealthy = outcome.status == "skipped" or (
                outcome.status == "failed" and outcome.error_kind not in ENTRY_SPECIFIC_KINDS
            )
            if unhealthy:
                reason = "provider still unhealthy"
                return results + [ReplayOutcome(i, "skipped", error=reason) for i in ids]

        sem = asyncio.Semaphore(max(1, concurrency))

        async def bounded(dlq_id: int) -> ReplayOutcome:
            async with sem:
                return await self.replay(dlq_id)

        return results + list(await asyncio.gather(*(bounded(i) for i in ids)))

    def _done(self, outcome: ReplayOutcome) -> ReplayOutcome:
        self._metrics.replays.labels(outcome.status).inc()
        log.info("dlq replay id=%s status=%s error=%s", outcome.id, outcome.status, outcome.error)
        return outcome
