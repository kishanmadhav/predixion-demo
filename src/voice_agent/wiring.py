"""Assemble a runtime (store, providers, resilient stages, pipeline, DLQ) from Settings.

Used by the HTTP app, the CLI and the tests, so all three run the exact same wiring.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from voice_agent.calls import CallTracker
from voice_agent.config import Settings
from voice_agent.dlq import DeadLetterService
from voice_agent.emf import EmfReporter, write_stdout
from voice_agent.fallback import load_fallbacks
from voice_agent.metrics import Metrics
from voice_agent.pipeline import TurnPipeline
from voice_agent.providers.factory import Providers, build_providers
from voice_agent.resilience.breaker import CircuitBreaker
from voice_agent.resilience.retry import RetryPolicy
from voice_agent.resilience.stage import ResilientStage
from voice_agent.store import Store, open_store

STAGE_NAMES = ("stt", "llm", "tts")

log = logging.getLogger(__name__)


def lease_seconds(settings: Settings) -> float:
    """How long a turn or a replay claim may sit untouched before it is reclaimed:
    comfortably longer than any turn can legitimately run."""
    return max(60.0, 3 * settings.turn_deadline_s)


@dataclass
class Runtime:
    settings: Settings
    store: Store
    metrics: Metrics
    providers: Providers
    stages: dict[str, ResilientStage]
    pipeline: TurnPipeline
    dlq: DeadLetterService
    recovered_dead_letters: list[int]
    calls: CallTracker
    emf: EmfReporter | None
    _client: httpx.AsyncClient | None

    @property
    def lease_s(self) -> float:
        """The sweeper's lease (see `lease_seconds`)."""
        return lease_seconds(self.settings)

    async def sweep(self) -> list[int]:
        """Reclaim work orphaned while the service kept running (a cancelled request,
        a failed write). Returns the dead-letter ids it created."""
        ids = await self.store.recover(stale_after_s=self.lease_s)
        for _ in ids:
            self.metrics.observe_dead_letter("interrupted")
        if ids:
            log.warning("sweeper dead-lettered %d stale turn(s): %s", len(ids), ids)
        return ids

    async def run_sweeper(self) -> None:
        while True:
            await asyncio.sleep(self.settings.sweep_interval_s)
            try:
                await self.sweep()
            except Exception:
                log.exception("lease sweep failed; will retry")

    async def run_emf(self, write: Callable[[dict[str, Any]], None] = write_stdout) -> None:
        """Flush EMF metrics every `emf_interval_s`; refresh DLQ depth once a minute."""
        assert self.emf is not None
        pending: int | None = None
        last_count = float("-inf")
        while True:
            await asyncio.sleep(self.settings.emf_interval_s)
            try:
                if time.monotonic() - last_count >= 60:
                    pending = (await self.store.dead_letter_counts())["pending"]
                    last_count = time.monotonic()
                for doc in self.emf.flush(
                    active_calls=self.calls.active(),
                    in_flight=self.calls.in_flight,
                    dlq_pending=pending,
                ):
                    write(doc)
            except Exception:
                log.exception("EMF flush failed; will retry")

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
        await self.store.close()


def build_stages(settings: Settings, metrics: Metrics) -> dict[str, ResilientStage]:
    timeouts = {
        "stt": settings.stt_timeout_s,
        "llm": settings.llm_timeout_s,
        "tts": settings.tts_timeout_s,
    }
    stages = {}
    for name in STAGE_NAMES:
        breaker = CircuitBreaker(
            name,
            window_size=settings.breaker_window,
            minimum_calls=min(settings.breaker_minimum_calls, settings.breaker_window),
            failure_rate_threshold=settings.breaker_failure_threshold,
            open_seconds=settings.breaker_open_s,
            half_open_max_calls=settings.breaker_half_open_calls,
            on_transition=metrics.observe_transition,
        )
        metrics.breaker_state.labels(name).set(0)
        stages[name] = ResilientStage(
            name,
            breaker=breaker,
            retry=RetryPolicy(
                max_attempts=settings.retry_max_attempts,
                base_delay=settings.retry_base_delay_s,
                max_delay=max(settings.retry_max_delay_s, settings.retry_base_delay_s),
                rng=random.Random(),
            ),
            timeout_s=timeouts[name],
            observer=metrics.observe_attempt,
        )
    return stages


def make_http_client() -> httpx.AsyncClient:
    # Per-attempt timeouts are enforced by ResilientStage; this is only a backstop.
    return httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=5.0),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
    )


async def open_runtime(
    settings: Settings,
    *,
    providers: Providers | None = None,
    client: httpx.AsyncClient | None = None,
    recover: bool = True,
) -> Runtime:
    """Open the store, run crash recovery, and wire everything together.

    Pass `providers` to inject fakes, or `client` to route the real adapters through
    a custom transport (tests use this to talk to the mock app in-process).
    `recover=False` is for tools (the CLI) that open the database while the service
    may be running: only the service may reclaim in-flight work.
    """
    store = await open_store(settings)
    try:
        return await _wire(settings, store, providers=providers, client=client, recover=recover)
    except BaseException:
        await store.close()
        raise


async def _wire(
    settings: Settings,
    store: Store,
    *,
    providers: Providers | None,
    client: httpx.AsyncClient | None,
    recover: bool,
) -> Runtime:
    # A shared store (DynamoDB) is used by other live tasks: at startup reclaim only
    # work that has outlived the lease, never another task's in-flight turns.
    stale_after = lease_seconds(settings) if store.shared else None
    recovered = await store.recover(stale_after_s=stale_after) if recover else []
    emf = (
        EmfReporter(namespace=settings.emf_namespace, service=settings.emf_service_name)
        if settings.emf_enabled
        else None
    )
    metrics = Metrics(emf=emf)
    for _ in recovered:
        metrics.observe_dead_letter("interrupted")

    owned_client = None
    if providers is None:
        if client is None:
            client = owned_client = make_http_client()
        providers = build_providers(settings, client)

    stages = build_stages(settings, metrics)
    pipeline = TurnPipeline(
        providers=providers,
        stages=stages,
        store=store,
        settings=settings,
        metrics=metrics,
        fallbacks=load_fallbacks(settings.fallback_audio_dir),
    )
    dlq = DeadLetterService(
        store=store, pipeline=pipeline, metrics=metrics, turn_deadline_s=settings.turn_deadline_s
    )
    return Runtime(
        settings=settings,
        store=store,
        metrics=metrics,
        providers=providers,
        stages=stages,
        pipeline=pipeline,
        dlq=dlq,
        recovered_dead_letters=recovered,
        calls=CallTracker(settings.call_idle_s),
        emf=emf,
        _client=owned_client,
    )
