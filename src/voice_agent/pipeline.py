"""TurnPipeline: one caller turn through STT -> LLM -> TTS, never dropped.

Guarantees, in order of importance:

1. Every turn is written ahead (`in_progress`) before any provider is called.
2. Every turn ends `completed` or `degraded`; a degraded turn's dead-letter row is
   committed (same transaction) *before* the caller hears the fallback prompt.
   This holds for provider failures, open circuits, blown deadlines and our own
   bugs. A process crash is covered by `Store.recover()` at the next startup.
3. The caller always gets audio back: the real reply, or a pre-rendered fallback
   plus an `action` telling the telephony layer to re-prompt or hand off.
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
from voice_agent.resilience.stage import AttemptRecord, ResilientStage, StageFailedError
from voice_agent.store import Store, Turn, TurnStatus

__all__ = ["Action", "ProcessedTurn", "TurnFailedError", "TurnPipeline", "TurnResult"]

log = logging.getLogger(__name__)
T = TypeVar("T")

_HISTORY_STATUSES = (TurnStatus.COMPLETED, TurnStatus.COMPLETED_ON_REPLAY)


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

    # -- live turns ---------------------------------------------------------

    async def handle_turn(self, call_id: str, turn_id: str, audio: bytes) -> TurnResult:
        started = self._clock()
        request = {"audio_b64": base64.b64encode(audio).decode("ascii")}
        if not await self.store.begin_turn(call_id, turn_id, request):
            return await self._duplicate(call_id, turn_id)

        try:
            processed = await self.process(
                call_id, turn_id, audio, deadline=started + self.settings.turn_deadline_s
            )
        except TurnFailedError as failure:
            return await self._degrade(call_id, turn_id, failure, started)
        except asyncio.CancelledError:
            # Client went away or the server is shutting down: still leave a record.
            failure = TurnFailedError("pipeline", "cancelled", "turn was cancelled", [], {})
            await asyncio.shield(self._dead_letter(call_id, turn_id, failure))
            raise
        except Exception as exc:  # a bug must not lose the turn either
            log.exception("unexpected error in turn %s/%s", call_id, turn_id)
            failure = TurnFailedError("pipeline", "internal_error", repr(exc), [], {})
            return await self._degrade(call_id, turn_id, failure, started)

        await self.store.complete_turn(call_id, turn_id, processed.record(), processed.attempts)
        duration = self._clock() - started
        self.metrics.turns.labels(TurnStatus.COMPLETED.value).inc()
        self.metrics.turn_seconds.labels(TurnStatus.COMPLETED.value).observe(duration)
        return TurnResult(
            call_id=call_id,
            turn_id=turn_id,
            status=TurnStatus.COMPLETED,
            action=Action.CONTINUE,
            transcript=processed.transcript,
            reply_text=processed.reply_text,
            audio=processed.audio,
            attempts=processed.attempts,
            duration_ms=round(duration * 1000, 1),
        )

    # -- shared by live turns and DLQ replay ----------------------------------

    async def process(
        self, call_id: str, turn_id: str, audio: bytes, *, deadline: float
    ) -> ProcessedTurn:
        """Run the three stages. Raises TurnFailedError with partial results on failure."""
        attempts: list[AttemptRecord] = []
        partial: dict[str, Any] = {}
        history = await self._history(call_id, before_turn=turn_id)
        p = self.providers

        transcript = await self._run("stt", lambda: p.stt.transcribe(audio), deadline,
                                     attempts, partial)
        partial["transcript"] = transcript
        messages = [
            ChatMessage("system", self.settings.system_prompt),
            *history,
            ChatMessage("user", transcript),
        ]
        reply = await self._run("llm", lambda: p.llm.complete(messages), deadline,
                                attempts, partial)
        partial["reply_text"] = reply
        speech = await self._run("tts", lambda: p.tts.synthesize(reply), deadline,
                                 attempts, partial)
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
                name, exc.error.kind.value, str(exc.error),
                [a.to_dict() for a in attempts], dict(partial),
            ) from exc
        attempts.extend(outcome.attempts)
        return outcome.value

    async def _history(self, call_id: str, *, before_turn: str) -> list[ChatMessage]:
        if self.settings.llm_history_turns == 0:
            return []
        prior: list[Turn] = []
        for turn in await self.store.list_turns(call_id):
            if turn.turn_id == before_turn:
                break
            if turn.status in _HISTORY_STATUSES and turn.result:
                prior.append(turn)
        messages = []
        for turn in prior[-self.settings.llm_history_turns:]:
            assert turn.result is not None
            messages.append(ChatMessage("user", turn.result["transcript"]))
            messages.append(ChatMessage("assistant", turn.result["reply_text"]))
        return messages

    # -- degradation ----------------------------------------------------------

    async def _dead_letter(self, call_id: str, turn_id: str, failure: TurnFailedError) -> int:
        dlq_id = await self.store.degrade_turn(
            call_id,
            turn_id,
            failed_stage=failure.failed_stage,
            error_kind=failure.error_kind,
            error_detail=failure.detail,
            attempts=failure.attempts,
            partial=failure.partial,
        )
        self.metrics.dead_letters.labels("stage_failed").inc()
        self.metrics.turns.labels(TurnStatus.DEGRADED.value).inc()
        log.warning(
            "turn degraded call=%s turn=%s stage=%s kind=%s dlq_id=%s",
            call_id, turn_id, failure.failed_stage, failure.error_kind, dlq_id,
        )
        return dlq_id

    async def _degrade(
        self, call_id: str, turn_id: str, failure: TurnFailedError, started: float
    ) -> TurnResult:
        dlq_id = await self._dead_letter(call_id, turn_id, failure)
        consecutive = await self.store.consecutive_degraded(call_id)
        action = (
            Action.HANDOFF
            if consecutive >= self.settings.handoff_after_degraded
            else Action.RETRY_PROMPT
        )
        prompt = self.fallbacks[action]
        duration = self._clock() - started
        self.metrics.turn_seconds.labels(TurnStatus.DEGRADED.value).observe(duration)
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
        """A turn id we have already seen: report it, never re-run providers."""
        turn = await self.store.get_turn(call_id, turn_id)
        assert turn is not None
        result = turn.result or {}
        return TurnResult(
            call_id=call_id,
            turn_id=turn_id,
            status=turn.status,
            action=Action.CONTINUE if turn.status is TurnStatus.COMPLETED else Action.RETRY_PROMPT,
            transcript=result.get("transcript"),
            reply_text=result.get("reply_text"),
            audio=None,
            failed_stage=turn.failed_stage,
            error_kind=turn.error_kind,
            attempts=turn.attempts,
            duplicate=True,
        )
