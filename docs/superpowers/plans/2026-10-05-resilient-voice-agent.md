# Resilient Voice Agent Implementation Plan

> **For agentic workers:** executed inline (superpowers:executing-plans) by the author of the spec.
> Steps use checkbox (`- [ ]`) syntax for tracking. Each task: failing tests → implementation → green → commit.

**Goal:** Local collections voice-agent service with retry + circuit breaking around an STT→LLM→TTS
dependency, a replayable SQLite dead-letter queue, a model-agnostic provider boundary, and a Fargate
cost model.

**Architecture:** FastAPI orchestrator; per-stage `ResilientStage` (retry ∘ breaker ∘ timeout)
wrapping `Protocol` providers; SQLite (aiosqlite, WAL) audit log + DLQ written transactionally before
responding; separate FastAPI mock provider with chaos controls.

**Tech Stack:** Python 3.11+, uv, FastAPI, httpx, aiosqlite, pydantic-settings, prometheus-client,
pytest/pytest-asyncio, ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-10-05-resilient-voice-agent-design.md`

## Global Constraints

- Python `>=3.11` (uses `asyncio.timeout`); setup is `uv sync` only; Docker optional.
- No secrets in code or committed files; keys are `SecretStr` from env, `.env` git-ignored, `.env.example` blank.
- Resilience code never imports httpx or adapter modules; adapters never import resilience.
- Every turn ends in exactly one of: `completed`, `degraded`, `interrupted`, `completed_on_replay`; every non-completed turn has a DLQ row.
- Defaults: retry `max_attempts=3, base=0.1s, cap=2s`; breaker `window=50, minimum_calls=20, threshold=0.5, open=5s, half_open_probes=5`; `HANDOFF_AFTER_DEGRADED=2`; mock `FAILURE_RATE=0.2`.
- Commands documented must work on Linux/macOS/Windows (no bash-only scripts, no Makefile dependency).

---

### Task 1: Scaffold + error taxonomy + provider Protocols

**Files:** `pyproject.toml`, `.gitignore`, `.env.example`, `src/voice_agent/__init__.py`,
`src/voice_agent/providers/base.py`, `tests/conftest.py`, `tests/test_errors.py`

**Produces:**
- `ErrorKind(StrEnum)`: `TIMEOUT, CONNECTION, RATE_LIMITED, SERVER_ERROR, MALFORMED, CLIENT_ERROR, AUTH, CIRCUIT_OPEN, DEADLINE`
- `ProviderError(Exception)(stage: str, kind: ErrorKind, message: str, status_code: int|None=None, retry_after: float|None=None)`; property `retryable`, `counts_as_failure`
- `SttProvider.transcribe(audio: bytes) -> str`; `LlmProvider.complete(messages: list[ChatMessage]) -> str`; `TtsProvider.synthesize(text: str) -> bytes`; `ChatMessage(role, content)` dataclass

- [ ] tests: transient kinds retryable & counted; CLIENT_ERROR/AUTH neither; CIRCUIT_OPEN/DEADLINE not retryable
- [ ] implement, `uv sync`, green, commit

### Task 2: RetryPolicy

**Files:** `src/voice_agent/resilience/retry.py`, `tests/resilience/test_retry.py`

**Produces:** `RetryPolicy(max_attempts, base_delay, max_delay, rng: Random)` with
`backoff(attempt_index: int, error: ProviderError) -> float` (full jitter, honours `retry_after` capped by `max_delay`).

- [ ] tests: delays bounded by `min(cap, base*2^n)`; seeded RNG deterministic; Retry-After honoured and capped
- [ ] implement, green, commit

### Task 3: CircuitBreaker

**Files:** `src/voice_agent/resilience/breaker.py`, `tests/resilience/test_breaker.py`

**Produces:** `BreakerState(StrEnum)`; `CircuitBreaker(name, window_size, minimum_calls, failure_rate_threshold, open_seconds, half_open_max_calls, clock, on_transition)`;
methods `acquire() -> None` (raises `ProviderError(kind=CIRCUIT_OPEN)`), `record_success()`, `record_failure()`, `release()` (permit returned w/o outcome), property `state`, `snapshot() -> dict`.

- [ ] tests: stays closed below minimum_calls; stays closed at 20% failures over 50; trips at ≥50%; open rejects; after open_seconds → half-open admits exactly N probes; all-success → closed & window reset; failing probes → open again; transition callback fired
- [ ] implement, green, commit

### Task 4: ResilientStage

**Files:** `src/voice_agent/resilience/stage.py`, `tests/resilience/test_stage.py`

**Produces:** `AttemptRecord(attempt, outcome, error_kind, status_code, duration_ms, backoff_ms)`;
`StageFailedError(stage, error: ProviderError, attempts: list[AttemptRecord])`;
`ResilientStage(name, breaker, retry, timeout_s, sleep, clock, observer)`;
`async run(fn: Callable[[], Awaitable[T]], deadline: float) -> StageOutcome[T]` (`value`, `attempts`).
Per-attempt `asyncio.timeout(min(timeout_s, remaining))` → TIMEOUT. Non-ProviderError exceptions propagate (bugs are not retried).

- [ ] tests: succeeds after transient failures (attempt records); non-retryable stops at 1; open breaker fails fast without calling fn; timeout converted; deadline prevents further sleep/attempt; breaker records only counted failures
- [ ] implement, green, commit

### Task 5: SQLite store

**Files:** `src/voice_agent/store.py`, `tests/test_store.py`

**Produces:** `Store.open(path) -> Store`, `close()`; `begin_turn(call_id, turn_id, request: dict) -> bool` (False if exists);
`get_turn`, `list_turns(call_id)`; `complete_turn(call_id, turn_id, result: dict, attempts, status="completed")`;
`degrade_turn(call_id, turn_id, *, status, failed_stage, error_kind, error_detail, attempts, partial) -> int` (turn update + DLQ insert in one transaction, returns dlq id);
`recover_interrupted() -> list[int]`; `consecutive_degraded(call_id) -> int`;
DLQ: `list_dead_letters(status, limit)`, `get_dead_letter(id)`, `claim_dead_letter(id) -> DeadLetter|None`, `resolve_dead_letter(id, result, attempts)`, `release_dead_letter(id, error)`, `count_dead_letters(status)`.

- [ ] tests: write-ahead row; degrade is atomic and creates DLQ row; duplicate turn rejected; claim is exclusive; release increments replay_count; recover_interrupted dead-letters in_progress rows
- [ ] implement, green, commit

### Task 6: Adapters (mock + OpenAI-compatible) + factory + config

**Files:** `src/voice_agent/config.py`, `providers/http.py`, `providers/mock.py`, `providers/openai_compat.py`, `providers/factory.py`, `tests/providers/test_*.py`

**Produces:** `classify_response(stage, response)`, `classify_exception(stage, exc)`, `Settings` (pydantic-settings), `build_providers(settings, client_factory) -> Providers(stt, llm, tts)`.

- [ ] tests via `httpx.MockTransport`: request shapes (paths, multipart field `file`/`model`, chat body, speech body), Bearer only when key set, 429 w/ Retry-After → RATE_LIMITED+retry_after, 503 → SERVER_ERROR, 401/403 → AUTH, 400 → CLIENT_ERROR, bad JSON → MALFORMED, `httpx.ConnectError` → CONNECTION, factory picks adapters per stage
- [ ] implement, green, commit

### Task 7: Mock provider service

**Files:** `src/mock_provider/app.py`, `tests/test_mock_provider.py`

**Produces:** `create_app(MockConfig) -> FastAPI`; endpoints `/v1/stt`, `/v1/llm`, `/v1/tts`, `/admin/chaos`, `/admin/stats`, `/admin/reset`, `/health`.

- [ ] tests: failure_rate 0 → always 200; failure_rate 1 → all failures; outage per stage with duration; stats count outcomes; seeded determinism
- [ ] implement, green, commit

### Task 8: Pipeline + fallback + metrics + DLQ service

**Files:** `src/voice_agent/pipeline.py`, `fallback.py`, `metrics.py`, `dlq.py`, `tests/test_pipeline.py`, `tests/test_dlq.py`

**Produces:** `TurnPipeline(providers, stages, store, settings, metrics)`, `async handle_turn(call_id, turn_id, audio: bytes) -> TurnResult`, `async process(call_id, request) -> ProcessedTurn` (shared by live + replay); `DeadLetterService.replay(id) -> ReplayResult`, `replay_pending(limit)`.

- [ ] tests (fake providers): happy path completed; LLM exhausted → degraded + DLQ row with transcript partial + fallback audio; 2 consecutive degraded → `handoff`; replay success resolves and turn `completed_on_replay`; replay failure returns to pending; resolved entry not replayable
- [ ] implement, green, commit

### Task 9: API + CLI + end-to-end

**Files:** `src/voice_agent/api.py`, `cli.py`, `tests/test_api.py`, `tests/test_e2e.py`

- [ ] e2e (in-process mock via ASGITransport): 20% failure, 100 turns → all accounted, mock requests > turns×3; forced LLM outage → breaker opens, mock LLM request count plateaus, recovers to closed after outage; DLQ replay resolves after recovery; `/health` 200 while breakers open; idempotent duplicate turn returns stored result
- [ ] CLI `serve`, `mock`, `dlq list|show|replay`, `loadtest`, `chaos-demo`
- [ ] implement, green, commit

### Task 10: Cost model + docs + packaging

**Files:** `cost/fargate_cost.py`, `tests/test_cost.py`, `docs/COST.md`, `README.md`, `Dockerfile`, `docker-compose.yml`, `docker-compose.openweight.yml`, `.github/workflows/ci.yml`

- [ ] cost calculator tests (hand-computed numbers); write-up; README (setup, run, demo, design decisions, swap paragraph); compose files; CI
- [ ] full verification: ruff, mypy, pytest, live run of service + mock + loadtest + chaos-demo; commit
