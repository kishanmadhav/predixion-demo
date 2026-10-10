"""TurnPipeline: one caller turn through STT -> LLM -> TTS, never dropped.

Guarantees, in order of importance:

1. Every turn is written ahead (`in_progress`) before any provider is called.
2. Every turn ends `completed` or `degraded`; a degraded turn's dead-letter row is
   committed (same transaction) *before* the caller hears the fallback prompt.
   This holds for provider failures, open circuits, blown deadlines, our own bugs
   and cancellation: finalization runs shielded, so a cancelled request still
   records its outcome.
3. The caller always gets audio back: the real reply, or a pre-rendered fallback
   plus an `action` telling the telephony layer to re-prompt or hand off. Even a
   failed database write does not turn into a dropped call; the turn is then left
   `in_progress`, and the store's lease sweeper (or startup recovery) dead-letters it.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from voice_agent.config import Settings
from voice_agent.fallback import Action, FallbackPrompt
from voice_agent.metrics import Metrics
from voice_agent.providers.base import ChatMessage
from voice_agent.providers.factory import Providers
from voice_agent.resilience.breaker import BreakerState
from voice_agent.resilience.stage import AttemptRecord, ResilientStage, StageFailedError
from voice_agent.store import Store, TurnStatus

__all__ = ["Action", "ProcessedTurn", "TurnFailedError", "TurnPipeline", "TurnResult"]

log = logging.getLogger(__name__)
T = TypeVar("T")


@dataclass(frozen=True)
class ProcessedTurn:
    transcript: str
    reply_text: str
    audio: bytes
    attempts: list[dict[str, Any]]

    def record(self) -> dict[str, Any]:
        """What the audit log keeps (text, not audio)."""
        return {
            "transcript": self.transcript,
            "reply_text": self.reply_text,
            "audio_bytes": len(self.audio),
        }


class TurnFailedError(Exception):
    """A turn could not be completed. Carries everything the dead letter needs."""

    def __init__(
        self,
        failed_stage: str,
        error_kind: str,
        detail: str,
        attempts: list[dict[str, Any]],
        partial: dict[str, Any],
    ) -> None:
        super().__init__(detail)
        self.failed_stage = failed_stage
        self.error_kind = error_kind
        self.detail = detail
        self.attempts = attempts
        self.partial = partial


@dataclass(frozen=True)
class TurnResult:
    call_id: str
    turn_id: str
    status: TurnStatus
    action: Action
    reply_text: str | None
    audio: bytes | None
    transcript: str | None = None
    dlq_id: int | None = None
    failed_stage: str | None = None
    error_kind: str | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: float = 0.0
    duplicate: bool = False


class TurnPipeline:
    def __init__(
        self,
        *,
        providers: Providers,
        stages: dict[str, ResilientStage],
        store: Store,
        settings: Settings,
        metrics: Metrics,
        fallbacks: dict[Action, FallbackPrompt],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.providers = providers
        self.stages = stages
        self.store = store
        self.settings = settings
        self.metrics = metrics
        self.fallbacks = fallbacks
        self._clock = clock

    def circuits_closed(self) -> bool:
        return all(s.breaker.state is BreakerState.CLOSED for s in self.stages.values())

    def open_circuits(self) -> list[str]:
        return [n for n, s in self.stages.items() if s.breaker.state is BreakerState.OPEN]

    # -- live turns ---------------------------------------------------------

    async def handle_turn(self, call_id: str, turn_id: str, audio: bytes) -> TurnResult:
        started = self._clock()
        request = {"audio_b64": base64.b64encode(audio).decode("ascii")}
        try:
            is_new = await self.store.begin_turn(call_id, turn_id, request)
        except Exception:
            # Storage is down: don't process a turn we cannot record, but don't drop the
            # call either. This is the one case with no record; it is logged loudly.
            log.exception(
                "could not write ahead turn %s/%s; playing the fallback", call_id, turn_id
            )
            prompt = self.fallbacks[Action.RETRY_PROMPT]
            self.metrics.observe_turn("unrecorded", None)
            return TurnResult(
                call_id=call_id,
                turn_id=turn_id,
                status=TurnStatus.DEGRADED,
                action=Action.RETRY_PROMPT,
                reply_text=prompt.text,
                audio=prompt.audio,
                failed_stage="storage",
                error_kind="storage_unavailable",
                duration_ms=round((self._clock() - started) * 1000, 1),
            )
        if not is_new:
            return await self._duplicate(call_id, turn_id)

        outcome: ProcessedTurn | TurnFailedError
        try:
            outcome = await self.process(
                call_id, turn_id, audio, deadline=started + self.settings.turn_deadline_s
            )
        except TurnFailedError as failure:
            outcome = failure
        except asyncio.CancelledError:
            # Server shutdown: nobody will hear a reply, but the turn is still recorded.
            cancelled = TurnFailedError("pipeline", "cancelled", "turn was cancelled", [], {})
            await asyncio.shield(self._degrade(call_id, turn_id, cancelled, started))
            raise
        except Exception as exc:  # a bug outside any stage must not lose the turn either
            log.exception("unexpected error in turn %s/%s", call_id, turn_id)
            outcome = TurnFailedError("pipeline", "internal_error", repr(exc), [], {})

        # Shielded: once providers have answered, recording the outcome always finishes.
        return await asyncio.shield(self._finish(call_id, turn_id, outcome, started))

    async def _finish(
        self,
        call_id: str,
        turn_id: str,
        outcome: ProcessedTurn | TurnFailedError,
        started: float,
    ) -> TurnResult:
        if isinstance(outcome, TurnFailedError):
            return await self._degrade(call_id, turn_id, outcome, started)
        try:
            if not await self.store.complete_turn(
                call_id, turn_id, outcome.record(), outcome.attempts
            ):
                log.error(
                    "turn %s/%s was no longer in progress when it completed", call_id, turn_id
                )
        except Exception:
            # The caller still gets the reply; the lease sweeper dead-letters the turn.
            log.exception("could not record completed turn %s/%s", call_id, turn_id)
        duration = self._clock() - started
        self.metrics.observe_turn(TurnStatus.COMPLETED.value, duration)
        return TurnResult(
            call_id=call_id,
            turn_id=turn_id,
            status=TurnStatus.COMPLETED,
            action=Action.CONTINUE,
            transcript=outcome.transcript,
            reply_text=outcome.reply_text,
            audio=outcome.audio,
            attempts=outcome.attempts,
            duration_ms=round(duration * 1000, 1),
        )

    # -- shared by live turns and DLQ replay ----------------------------------

    async def process(
        self, call_id: str, turn_id: str, audio: bytes, *, deadline: float
    ) -> ProcessedTurn:
        """Run the three stages. Raises TurnFailedError with partial results on failure."""
        attempts: list[AttemptRecord] = []
        partial: dict[str, Any] = {}
        history = await self.store.history_before(
            call_id, turn_id, limit=self.settings.llm_history_turns
        )
        p = self.providers

        transcript = await self._run(
            "stt", lambda: p.stt.transcribe(audio), deadline, attempts, partial
        )
        partial["transcript"] = transcript
        messages = [ChatMessage("system", self.settings.system_prompt)]
        for said, replied in history:
            messages += [ChatMessage("user", said), ChatMessage("assistant", replied)]
        messages.append(ChatMessage("user", transcript))
        reply = await self._run(
            "llm", lambda: p.llm.complete(messages), deadline, attempts, partial
        )
        partial["reply_text"] = reply
        speech = await self._run(
            "tts", lambda: p.tts.synthesize(reply), deadline, attempts, partial
        )
        return ProcessedTurn(transcript, reply, speech, [a.to_dict() for a in attempts])

    async def _run(
        self,
        name: str,
        fn: Callable[[], Awaitable[T]],
        deadline: float,
        attempts: list[AttemptRecord],
        partial: dict[str, Any],
    ) -> T:
        try:
            outcome = await self.stages[name].run(fn, deadline=deadline)
        except StageFailedError as exc:
            attempts.extend(exc.attempts)
            raise TurnFailedError(
                name,
                exc.error.kind.value,
                str(exc.error),
                [a.to_dict() for a in attempts],
                dict(partial),
            ) from exc
        except Exception as exc:  # an adapter bug: keep the context gathered so far
            log.exception("unexpected error in stage %s", name)
            raise TurnFailedError(
                name, "internal_error", repr(exc), [a.to_dict() for a in attempts], dict(partial)
            ) from exc
        attempts.extend(outcome.attempts)
        return outcome.value

    # -- degradation ----------------------------------------------------------

    async def _degrade(
        self, call_id: str, turn_id: str, failure: TurnFailedError, started: float
    ) -> TurnResult:
        try:
            # This turn is still in progress, so it is not counted yet: add it.
            consecutive = await self.store.consecutive_degraded(call_id) + 1
        except Exception:
            log.exception("could not read call history for %s", call_id)
            consecutive = 1
        action = (
            Action.HANDOFF
            if consecutive >= self.settings.handoff_after_degraded
            else Action.RETRY_PROMPT
        )
        dlq_id = None
        try:
            dlq_id = await self.store.degrade_turn(
                call_id,
                turn_id,
                failed_stage=failure.failed_stage,
                error_kind=failure.error_kind,
                error_detail=failure.detail,
                attempts=failure.attempts,
                partial=failure.partial,
                action=action.value,
            )
            self.metrics.observe_dead_letter("stage_failed")
        except Exception:
            # Still play the fallback; the lease sweeper dead-letters the turn later.
            log.exception("could not dead-letter turn %s/%s", call_id, turn_id)
        log.warning(
            "turn degraded call=%s turn=%s stage=%s kind=%s dlq_id=%s action=%s",
            call_id,
            turn_id,
            failure.failed_stage,
            failure.error_kind,
            dlq_id,
            action.value,
        )
        prompt = self.fallbacks[action]
        duration = self._clock() - started
        self.metrics.observe_turn(TurnStatus.DEGRADED.value, duration)
        return TurnResult(
            call_id=call_id,
            turn_id=turn_id,
            status=TurnStatus.DEGRADED,
            action=action,
            transcript=failure.partial.get("transcript"),
            reply_text=prompt.text,
            audio=prompt.audio,
            dlq_id=dlq_id,
            failed_stage=failure.failed_stage,
            error_kind=failure.error_kind,
            attempts=failure.attempts,
            duration_ms=round(duration * 1000, 1),
        )

    async def _duplicate(self, call_id: str, turn_id: str) -> TurnResult:
        """A turn id we have already seen: report what the caller got, never re-run
        providers. Completed turns return the reply text (audio is not stored);
        anything else returns the fallback prompt the caller should hear."""
        turn = await self.store.get_turn(call_id, turn_id)
        assert turn is not None
        result = turn.result or {}
        if turn.status is TurnStatus.COMPLETED:
            return TurnResult(
                call_id=call_id,
                turn_id=turn_id,
                status=turn.status,
                action=Action.CONTINUE,
                transcript=result.get("transcript"),
                reply_text=result.get("reply_text"),
                audio=None,
                attempts=turn.attempts,
                duplicate=True,
            )
        action = Action.HANDOFF if turn.action == Action.HANDOFF.value else Action.RETRY_PROMPT
        prompt = self.fallbacks[action]
        return TurnResult(
            call_id=call_id,
            turn_id=turn_id,
            status=turn.status,
            action=action,
            transcript=result.get("transcript"),
            reply_text=prompt.text,
            audio=prompt.audio,
            failed_stage=turn.failed_stage,
            error_kind=turn.error_kind,
            attempts=turn.attempts,
            duplicate=True,
        )
