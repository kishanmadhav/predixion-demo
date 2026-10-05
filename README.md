# Resilient collections voice agent

A collections voice-agent inference service. Each caller turn goes through **STT → LLM → TTS**, and a resilience layer around the pipeline keeps a flaky provider from dropping the call:
- **Retries** with exponential backoff and full jitter.
- **A circuit breaker for each stage.**
- **Graceful degradation** to a pre-rendered fallback prompt.
- **A dead-letter queue** that lives in SQLite and can be inspected and replayed. Every failed turn is written to it *before* the caller hears the fallback.

A local mock provider stands in for STT/LLM/TTS. It adds realistic latency, fails 20% of requests, and can force outages on demand. The provider boundary is model-agnostic: open-weight models (faster-whisper, Ollama/vLLM/llama.cpp, Kokoro) replace the mock through configuration alone.

| Brief | Where |
|---|---|
| Resilience layer: retries, backoff, circuit breaking | [`resilience/`](src/voice_agent/resilience), [§ Failure handling](#failure-handling) |
| Dead letters that can be inspected and replayed | [`store.py`](src/voice_agent/store.py), [`dlq.py`](src/voice_agent/dlq.py), [§ Dead-letter queue](#dead-letter-queue) |
| Model-agnostic dependency boundary | [`providers/`](src/voice_agent/providers), [§ Swapping in open-weight models](#swapping-in-open-weight-models) |
| Cost reasoning (half page) | [`docs/COST.md`](docs/COST.md), [`cost/fargate_cost.py`](cost/fargate_cost.py) |
| Design spec | [`docs/superpowers/specs/…-design.md`](docs/superpowers/specs/2026-10-05-resilient-voice-agent-design.md) |

## Quick start

You need Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/). No AWS account, API keys or GPU are needed.

```bash
uv sync                                   # install
uv run pytest -q                          # 161 tests, ~10 s

# terminal 1: mock STT/LLM/TTS provider, 20% failure rate
uv run voice-agent mock --port 9000
# terminal 2: the service
uv run voice-agent serve                  # http://127.0.0.1:8080, docs at /docs
# terminal 3: traffic
uv run voice-agent loadtest               # 50 calls x 4 turns, compares both sides
uv run voice-agent chaos-demo             # baseline -> LLM outage -> recovery -> replay
```

With Docker instead: `docker compose up --build`, then run `loadtest` / `chaos-demo` from the host.

One turn by hand:

```bash
curl -s localhost:8080/v1/calls/call-1/turns -H 'content-type: application/json' \
  -d '{"turn_id": "t1", "audio_b64": "aGVsbG8="}'
```

<details><summary>Output from one local run (Windows laptop, real processes)</summary>

`loadtest`:

```
turns sent:          240
  completed:         237
  degraded:          3  (handoff: 0)
  HTTP failures:     0   <- should always be 0
degradation causes:  stt:server_error=1, stt:rate_limited=1, tts:rate_limited=1
breakers:            stt=closed, llm=closed, tts=closed
dead letters:        pending=3, replaying=0, resolved=0
provider received (requests / ok / failed):
  stt:   303 /   238 /    65
  llm:   293 /   238 /    55
  tts:   288 /   237 /    51
```

`chaos-demo` (excerpt). LLM traffic stays flat while its breaker is open (90 → 90 → 93, and the extra 3 are half-open probes), and every degraded turn is dead-lettered and later replayed:

```
  [outage t+3s ] breakers: stt=closed llm=open   tts=closed  provider requests: stt=85  llm=90  tts=69  dlq pending=16
  [outage t+6s ] breakers: stt=closed llm=open   tts=closed  provider requests: stt=99  llm=90  tts=69  dlq pending=26
  [outage t+9s ] breakers: stt=closed llm=open   tts=closed  provider requests: stt=112 llm=93  tts=69  dlq pending=36
  [recovery t+7s] breakers: stt=closed llm=closed tts=closed provider requests: stt=129 llm=101 tts=75  dlq pending=46
4) Replay the dead-letter queue now that the provider is healthy.
   replay results: {'resolved': 43, 'failed': 3}
```

The 3 failed replays hit the mock's normal 20% failure rate on every retry. They remain `pending` and can be replayed again.
</details>

## Architecture

```
POST /v1/calls/{call_id}/turns
  └─ TurnPipeline  (per-turn deadline, default 6 s)
       ├─ write-ahead: turn row = in_progress            ── SQLite (WAL, synchronous=FULL)
       ├─ ResilientStage("stt") = Retry(Breaker(Timeout(SttProvider.transcribe)))
       ├─ ResilientStage("llm") = Retry(Breaker(Timeout(LlmProvider.complete)))
       ├─ ResilientStage("tts") = Retry(Breaker(Timeout(TtsProvider.synthesize)))
       ├─ success → turn = completed, reply audio returned
       └─ failure → ONE transaction: turn = degraded + dead_letters row
                    → return pre-rendered fallback audio + action (retry_prompt | handoff)

providers/  base.py (Protocols + ProviderError)  ← the only thing resilience/ and pipeline know
            mock.py | openai_compat.py           ← adapters; factory.py picks one per stage
```

| Module | Responsibility |
|---|---|
| `providers/base.py` | `SttProvider` / `LlmProvider` / `TtsProvider` Protocols, plus `ProviderError(kind)`, the error taxonomy |
| `providers/http.py` | The only place that maps HTTP and transport failures to error kinds |
| `resilience/retry.py`, `breaker.py`, `stage.py` | Retry policy, circuit breaker, and the composition of the two. No HTTP, no adapter imports |
| `pipeline.py` | One turn end to end: degradation, handoff decision, call history for the LLM |
| `store.py`, `dlq.py` | Audit log, dead letters, crash recovery, replay |
| `api.py`, `cli.py`, `wiring.py` | HTTP API, operator CLI, and the single wiring that API, CLI and tests share |
| `mock_provider/app.py` | The mock "provider having a bad day" |

## Failure handling

**Error taxonomy.** Adapters turn every failure into a `ProviderError` with a `kind`:

| Kind | Source | Retried | Counts toward breaker |
|---|---|:-:|:-:|
| `timeout`, `connection` | attempt timeout, connect/read errors, HTTP 408 | ✓ | ✓ |
| `rate_limited` | 429 (honours `Retry-After`) | ✓ | ✓ |
| `server_error` | 5xx | ✓ | ✓ |
| `malformed` | a 2xx with an unparseable or wrong-shaped body | ✓ | ✓ |
| `client_error`, `auth` | 400/404/422, 401/403 | ✗ | ✗ |

A 4xx means *our* request or configuration is wrong. The provider itself is healthy, so a 4xx never trips the breaker. Bugs in our own code are not `ProviderError`s, so they are never retried, but they are still dead-lettered.

**Retry** ([`retry.py`](src/voice_agent/resilience/retry.py)):
- Up to 3 attempts. The delay is `uniform(0, min(2 s, 0.1 s · 2ⁿ))` (full jitter), so many calls failing together don't retry in lock-step.
- `Retry-After` is honoured, capped at the maximum delay.
- Retries stay inside the turn deadline: no backoff sleeps past it, no attempt starts with less than 50 ms left, and each attempt's timeout is `min(stage timeout, remaining budget)`. A hung provider costs at most one attempt timeout.

**Circuit breaker** ([`breaker.py`](src/voice_agent/resilience/breaker.py)), one per stage:
- **Opening:** it looks at a rolling window of the last 50 counted calls and opens when a *failure* brings the failure rate to ≥ 50% (with at least 20 calls in the window).
- **Why those thresholds:** the baseline 20% failure rate must not trip it, and with these settings that happens with probability ≈ 10⁻⁶ per window. A real outage trips it within about 20 attempts.
- **While open:** for 5 s, calls are rejected immediately with `circuit_open`. Nothing is sent to the provider and the caller sees no added latency. A rejection is never retried.
- **Half-open:** up to 5 probe calls are let through, and a majority decides. Mostly successes close the circuit with an empty window; mostly failures reopen it with a fresh cool-down. A single flaky probe can't cause flapping.
- **Stale results:** each permit carries the breaker's epoch, so a slow call admitted before a state change can't be miscounted as a probe.
- **Composition:** the order is `Retry(Breaker(attempt))`. Every attempt asks the breaker first, so an outage that starts mid-retry stops the retry loop.
- **Stages are independent:** a TTS outage doesn't stop STT. STT still runs during an LLM outage, so the dead letter records the caller's words.

**Degradation** ([`pipeline.py`](src/voice_agent/pipeline.py)). A turn is never dropped, and it is always recorded:
1. The turn row is written (`in_progress`) before any provider is called.
2. On failure, the turn is marked `degraded` and its dead-letter row is inserted, both in one transaction, before the response is sent. This covers provider errors, open circuits, blown deadlines, our own bugs, and cancellation (when the client disconnects).
3. The caller still gets **HTTP 200 with audio**: a pre-rendered fallback prompt that doesn't depend on the TTS stage that may have just failed, plus `action`. The action is `retry_prompt`, or `handoff` (transfer to a human / schedule a callback) after 2 consecutive degraded turns on the same call.
4. A process crash mid-turn leaves an `in_progress` row behind. At the next start-up, `Store.recover()` marks those turns `interrupted` and dead-letters them.

`/health` never depends on provider health. That way a provider outage can't make the load balancer recycle orchestrator tasks that are degrading correctly. It still reports breaker states and DLQ counts. `/metrics` exposes Prometheus counters for attempts, retries, breaker state and transitions, turns, dead letters and replays.

## Dead-letter queue

A dead letter is a row in the `dead_letters` table of `data/voice_agent.db`. It holds:
- the original request (the audio),
- the failed stage and error kind,
- **every attempt** (stage, attempt number, outcome, error, HTTP status, duration, backoff),
- partial results (for example, the transcript when only the LLM failed),
- replay bookkeeping.

```bash
uv run voice-agent dlq list [--status pending] [--json]   # table of entries + counts
uv run voice-agent dlq show 7                             # full entry incl. attempts
uv run voice-agent dlq replay 7 9                         # replay specific entries
uv run voice-agent dlq replay --all                       # replay every pending entry
sqlite3 data/voice_agent.db 'select id, failed_stage, error_kind, status from dead_letters'
```

The same operations are available over HTTP: `GET /v1/dlq`, `GET /v1/dlq/{id}`, `POST /v1/dlq/{id}/replay`, and `POST /v1/dlq/replay`. `GET /v1/calls/{call_id}` returns a call's full turn-by-turn audit trail.

**Replay semantics** ([`dlq.py`](src/voice_agent/dlq.py)):
- **Claiming:** an entry is claimed atomically (`pending → replaying`), so two operators can never replay it at the same time.
- **Running:** the stored turn re-runs through the *same* resilient pipeline and the same breakers. A provider that is still down fails fast instead of being hammered.
- **Success:** the turn becomes `completed_on_replay`, with the transcript and reply recorded. The entry is marked `resolved`.
- **Failure:** the entry returns to `pending`, with `replay_count + 1` and the error recorded.
- **Bulk replay:** it ramps up one entry at a time while any circuit is half-open. As soon as one replay fails it stops and marks the rest `skipped` (untouched). Once every circuit is closed it runs 8 entries at a time.
- **Purpose:** the live moment has passed, so a replay *completes the compliance record* (what the debtor said, what the agent would have said) and can drive a callback. It doesn't speak to the caller.

## Swapping in open-weight models

The resilience layer and pipeline depend only on three one-method Protocols in `providers/base.py` (`transcribe(audio) -> str`, `complete(messages) -> str`, `synthesize(text) -> bytes`) and on the `ProviderError` taxonomy. `providers/factory.py` is the only code that knows which adapter backs each stage.

To replace the mock with an open-weight stack, set `{STT,LLM,TTS}_PROVIDER=openai` and point `*_BASE_URL` at servers that speak the OpenAI-compatible API, which the main open-weight servers already expose:
- **STT:** [Speaches](https://speaches.ai) running faster-whisper (`/v1/audio/transcriptions`).
- **LLM:** vLLM, Ollama or llama.cpp `llama-server` serving Qwen or Llama (`/v1/chat/completions`).
- **TTS:** [Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI) (`/v1/audio/speech`).

The `openai_compat` adapters build those requests and classify those servers' failure codes. For example, Ollama's queue-full 503 and llama.cpp's loading 503 are retryable, while Speaches' 403 on a bad key is not. Stages can be mixed, for instance a real LLM with mock STT/TTS. [`docker-compose.openweight.yml`](docker-compose.openweight.yml) runs the whole stack on CPU with nothing but environment changes. A server with a different API, or an in-process model such as `faster_whisper.WhisperModel` run via `asyncio.to_thread`, needs one new ~40-line adapter that implements the Protocol and raises `ProviderError`. Retries, breakers, deadlines and dead-lettering then apply to it unchanged.

What I verified, and what I didn't:
- The adapters are contract-tested against the documented request and response shapes of each server (`tests/providers/test_adapters.py`).
- I did not run the open-weight compose stack in this environment, because it needs multi-GB model downloads.
- With real models, raise the timeouts (`LLM_TIMEOUT_S`, `TURN_DEADLINE_S`) to match measured latency.

## Configuration

Everything is set through environment variables (or `.env`). See [`.env.example`](.env.example) for the full list and its defaults. There are no secrets in the repo: API keys (`*_API_KEY`) are optional, have no default, and are held as `SecretStr` so they never appear in logs or reprs. A key is sent as `Authorization: Bearer` only when it is set. The mock reads `MOCK_FAILURE_RATE`, `MOCK_SEED`, `MOCK_LATENCY_SCALE` and `MOCK_HANG_S` from the process environment.

**Pointing at a different mock:**
- If your mock serves this JSON shape (`POST {base}/stt|llm|tts`), set `*_BASE_URL`.
- If it speaks the OpenAI-compatible API, set `*_PROVIDER=openai`.
- Otherwise, add an adapter in `providers/`. Nothing else changes.

The mock's `POST /admin/chaos {"stage": "llm", "outage_s": 30}`, `{"failure_rate": 0.5}`, `GET /admin/stats` and `POST /admin/reset` drive the scenarios shown above.

## Tests

`uv run pytest -q` runs 161 tests in about 10 s. CI also runs `ruff`, `ruff format --check`, `mypy --strict` and a Docker build.

- **Unit tests:** retry jitter bounds and determinism; every breaker transition, including stale-epoch outcomes and probe limits; and the stage composition, all with a fake clock.
- **Adapter contract tests:** run with `httpx.MockTransport`.
- **Store and DLQ tests:** atomicity, exclusive claims, crash recovery, replay backpressure.
- **End-to-end tests** ([`tests/test_e2e.py`](tests/test_e2e.py)): the real wiring runs against the real mock app in-process, and the assertions are made from the **provider's side**:
  - At 20% failure, every turn ends `completed` or `degraded` with a dead letter, and the provider sees retries.
  - During a forced outage, the provider's request count stays flat while the breaker is open.
  - The breaker half-opens and closes after recovery, and dead letters replay cleanly.
  - A hung provider is cut off by the deadline.

## Cost

Summary of [`docs/COST.md`](docs/COST.md). Assumptions: us-east-1 x86 on-demand, 4-minute calls, 20 calls per 1 vCPU / 2 GB task, and one spare task.

| Configuration | $/month |
|---|---:|
| A. Static fleet sized for peak (11 tasks, 24×7) | $396 |
| **B. 3 tasks + scheduled scale-out to 11 for the campaign window (recommended)** | **$138** |

## Production notes and next steps

- **Distributed state:** breaker state is per process, which suits Fargate tasks that each protect themselves. The dead-letter store would become a managed queue plus a table (SQS + DynamoDB, or Postgres) instead of a local SQLite file.
- **Data protection:** transcripts and audio in collections are sensitive. They need encryption at rest, a retention policy, and access audit on the DLQ.
- **Streaming:** real calls stream audio (WebSocket/RTP) with partial transcripts. The turn-level boundary used here stays the same, and the stage timeouts become time-to-first-token budgets.
- **Hedging:** hedged requests or a fallback provider per stage (for example, a smaller LLM when the primary's breaker is open) would let more turns complete instead of degrading.
