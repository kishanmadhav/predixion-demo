# Round 2 AWS Deploy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy the Round 1 voice-agent to AWS ap-south-1 as a production-shaped system: Terraform-only, Fargate behind an ALB, custom-metric autoscaling, one CloudWatch dashboard, a live campaign load test against open-weight models on one g5.xlarge with 20% fault injection, and a verified teardown.

**Architecture:** The service gains a pluggable store (SQLite locally, DynamoDB on AWS), call tracking with an `ActiveCalls` metric emitted as CloudWatch EMF log lines, and a chaos-proxy mode of the mock provider that injects the Round 1 failure mix in front of real model servers. Terraform builds:
- the VPC, NAT and gateway endpoints;
- ECR and the image build;
- the DynamoDB table and least-privilege IAM;
- the ALB → Fargate service, with target tracking on `ActiveCalls`;
- an internal NLB → model tier, either a mock-provider Fargate service (`model_tier = "mock"`) or a g5.xlarge ASG running docker compose with vLLM, Speaches and the chaos proxy (`model_tier = "gpu"`);
- the dashboard and alarms.

**Tech Stack:** Python 3.11+, FastAPI, httpx, aiosqlite, boto3, moto (tests), Terraform ≥ 1.9 (hashicorp/aws ~> 6.6, kreuzwerker/docker), Docker Desktop on Windows, AWS CLI v2.

**Spec:** `docs/superpowers/specs/2026-10-07-round2-aws-design.md`

## Global Constraints

- Region `ap-south-1`. AWS CLI profile `predixion` (account 353789345457). Every AWS CLI call passes `--profile predixion`, and Terraform uses `var.aws_profile = "predixion"`. Never use the `default` or `lamp_cicd` profiles.
- Spend: hard ceiling $100 (Free-plan credit). Working target ≈ $14; reimbursement cap ₹2,500 (≈ $30). Destroy the stack between sessions.
- IAM:
  - No IAM policy Terraform creates may contain a wildcard (`*`) action or `NotAction`.
  - Every policy is built with `data "aws_iam_policy_document"`.
  - No AWS-managed policy attachments.
  - `scripts/check_iam.py` enforces all three.
- Names:
  - app log group `/ecs/collectionsinference`;
  - metric namespace `CollectionsChallenge`;
  - ECS cluster `collectionsinference`;
  - ECS service `voice-agent`;
  - DynamoDB table `collectionsinference-state`.
- No secrets in code, tfvars or images. Model servers need no keys.
- Python:
  - `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy` (strict) and `uv run pytest -q` stay green after every task;
  - pytest runs with `filterwarnings = error`.
- Commit messages carry **no** `Co-Authored-By` or Claude attribution lines (standing user instruction).
- `docs/writeup.pdf` is never committed.
- Model defaults (Appendix E not provided):
  - LLM `Qwen/Qwen2.5-7B-Instruct-AWQ`, served as `qwen2.5-7b-instruct`;
  - STT `Systran/faster-whisper-small`;
  - TTS `speaches-ai/Kokoro-82M-v1.0-ONNX` with voice `af_heart`.
- GPU host:
  - vLLM `vllm/vllm-openai:v0.31.0` with `--quantization awq_marlin --gpu-memory-utilization 0.70 --max-model-len 8192 --max-num-seqs 16`;
  - Speaches `ghcr.io/speaches-ai/speaches:latest-cuda-12.6.3`.
- g5 is offered only in AZ IDs `aps1-az1` and `aps1-az3`. Subnets are chosen by AZ **ID**.

---

## File Structure

```
src/voice_agent/store/__init__.py     re-exports + open_store(settings)
src/voice_agent/store/records.py      TurnStatus, Turn, DeadLetter, JSON/time helpers
src/voice_agent/store/base.py         Store Protocol (the contract both backends meet)
src/voice_agent/store/sqlite.py       SqliteStore (today's store.py, moved)
src/voice_agent/store/dynamodb.py     DynamoStore + TABLE_SCHEMA
src/voice_agent/calls.py              CallTracker (active calls, in-flight turns)
src/voice_agent/emf.py                EmfReporter (CloudWatch Embedded Metric Format)
src/voice_agent/campaign.py           scripted campaign traffic (Poisson arrivals, sticky calls)
src/mock_provider/chaos.py            ChaosState + admin routes, shared by mock and proxy
src/mock_provider/proxy.py            chaos proxy in front of OpenAI-compatible servers
scripts/check_iam.py                  fail on wildcard IAM actions in Terraform JSON
scripts/teardown_check.py             prove nothing billable remains; writes teardown/*.log
scripts/chaos.py                      drive the GPU host's chaos proxy via SSM Run Command
scripts/make_utterances.ps1           synthesize assets/utterances/*.wav with Windows SAPI
cost/measured.py                      gross spend by service from Cost Explorer
infra/*.tf, infra/templates/*         Terraform
RUNBOOK.md, docs/demo-shot-list.md, docs/ap-south-1-notes.md
tests/store/{conftest,test_contract,test_sqlite,test_dynamodb}.py
tests/test_calls.py, tests/test_emf.py, tests/test_proxy.py, tests/test_campaign.py
tests/scripts/{test_check_iam,test_teardown_check}.py
```

---

### Task 1: Split the store into a package with a `Store` protocol (no behaviour change)

**Files:**
- Create: `src/voice_agent/store/__init__.py`, `src/voice_agent/store/records.py`, `src/voice_agent/store/base.py`, `src/voice_agent/store/sqlite.py`
- Delete: `src/voice_agent/store.py`
- Modify: `src/voice_agent/wiring.py`, `src/voice_agent/cli.py`, `src/voice_agent/pipeline.py:34`, `src/voice_agent/dlq.py:26`, `src/voice_agent/api.py:21`
- Create tests: `tests/store/__init__.py`, `tests/store/conftest.py`, `tests/store/test_contract.py`, `tests/store/test_sqlite.py`
- Delete: `tests/test_store.py` (its tests move, unchanged in substance)
- Modify: `tests/test_cli.py` (`Store.open` → `SqliteStore.open`)

**Interfaces:**
- Produces:
  - `voice_agent.store.Store` (Protocol);
  - `voice_agent.store.SqliteStore` with `async open(path) -> SqliteStore` and `shared: bool = False`;
  - `voice_agent.store.open_store(settings) -> Store` (async);
  - `TurnStatus`, `Turn`, `DeadLetter`, `DEAD_LETTER_STATUSES`, `LIVE_DEGRADED`, `now()`, `iso()`, `dumps()`, `loads()` from `voice_agent.store.records`, also re-exported from `voice_agent.store`.

- [ ] **Step 1: Create `records.py`.** Move into it from `store.py`:
  - `DEAD_LETTER_STATUSES`, `TurnStatus` and `_LIVE_DEGRADED`, renamed `LIVE_DEGRADED`;
  - `_iso`/`_now`/`_dumps`/`_loads`, renamed `iso`/`now`/`dumps`/`loads`;
  - the `Turn` and `DeadLetter` dataclasses **without** their `from_row` classmethods. Keep `DeadLetter.summary()`.

  Module docstring: `"""Records shared by every store backend: turn and dead-letter shapes, statuses, helpers."""`.

- [ ] **Step 2: Create `base.py`** with the protocol:

```python
"""The storage contract. Both backends (SQLite locally, DynamoDB on AWS) pass the same
contract tests in tests/store/test_contract.py."""

from __future__ import annotations

from typing import Any, Protocol

from voice_agent.store.records import DeadLetter, Turn, TurnStatus


class Store(Protocol):
    # True when several service instances share the store (DynamoDB). Startup recovery
    # must then only reclaim stale work, never another live instance's in-flight turns.
    shared: bool

    async def close(self) -> None: ...
    async def begin_turn(self, call_id: str, turn_id: str, request: dict[str, Any]) -> bool: ...
    async def get_turn(self, call_id: str, turn_id: str) -> Turn | None: ...
    async def list_turns(self, call_id: str) -> list[Turn]: ...
    async def history_before(
        self, call_id: str, turn_id: str, *, limit: int
    ) -> list[tuple[str, str]]: ...
    async def complete_turn(
        self, call_id: str, turn_id: str, result: dict[str, Any], attempts: list[dict[str, Any]]
    ) -> bool: ...
    async def degrade_turn(
        self,
        call_id: str,
        turn_id: str,
        *,
        failed_stage: str,
        error_kind: str,
        error_detail: str,
        attempts: list[dict[str, Any]],
        partial: dict[str, Any],
        action: str | None = None,
    ) -> int: ...
    async def consecutive_degraded(self, call_id: str) -> int: ...
    async def recover(self, *, stale_after_s: float | None = None) -> list[int]: ...
    async def get_dead_letter(self, dlq_id: int) -> DeadLetter | None: ...
    async def list_dead_letters(
        self, *, status: str | None = None, call_id: str | None = None, limit: int = 100
    ) -> list[DeadLetter]: ...
    async def dead_letter_counts(self) -> dict[str, int]: ...
    async def claim_dead_letter(self, dlq_id: int) -> DeadLetter | None: ...
    async def release_dead_letter(
        self, dlq_id: int, *, error: str, attempts: list[dict[str, Any]]
    ) -> bool: ...
    async def resolve_dead_letter(
        self, dlq_id: int, result: dict[str, Any], attempts: list[dict[str, Any]]
    ) -> bool: ...


__all__ = ["Store", "TurnStatus"]
```

- [ ] **Step 3: Create `sqlite.py`.**
  - Move the rest of `store.py` into it: the `SCHEMA` and `_ADDED_COLUMNS` constants and `SCHEMA_VERSION`; the class becomes `SqliteStore`. Keep the module docstring from `store.py`.
  - Add the class attribute `shared = False`.
  - Replace `Turn.from_row(row)` / `DeadLetter.from_row(row)` with the module functions `_turn(row: aiosqlite.Row) -> Turn` and `_dead_letter_row(row: aiosqlite.Row) -> DeadLetter`, whose bodies are the old classmethods.
  - Use `records.now/iso/dumps/loads/LIVE_DEGRADED`.

- [ ] **Step 4: Create `__init__.py`:**

```python
"""Persistence: the per-turn audit log and the dead-letter queue, behind one contract."""

from __future__ import annotations

from typing import TYPE_CHECKING

from voice_agent.store.base import Store
from voice_agent.store.records import (
    DEAD_LETTER_STATUSES,
    LIVE_DEGRADED,
    DeadLetter,
    Turn,
    TurnStatus,
)
from voice_agent.store.sqlite import SqliteStore

if TYPE_CHECKING:
    from voice_agent.config import Settings


async def open_store(settings: Settings) -> Store:
    """The backend `settings.store_backend` names (SQLite unless configured)."""
    return await SqliteStore.open(settings.db_path)


__all__ = [
    "DEAD_LETTER_STATUSES",
    "LIVE_DEGRADED",
    "DeadLetter",
    "SqliteStore",
    "Store",
    "Turn",
    "TurnStatus",
    "open_store",
]
```

- [ ] **Step 5: Update the callers.**
  - `wiring.py`: `store = await open_store(settings)` replaces `Store.open(settings.db_path)`; the import becomes `from voice_agent.store import Store, open_store`.
  - `cli.py` `_dlq`: `store = await open_store(settings)`.
  - `pipeline.py`, `dlq.py`, `api.py`: imports are unchanged; `Store` is now the protocol.
  - Delete `src/voice_agent/store.py`.

- [ ] **Step 6: Split the tests.**
  - `tests/store/conftest.py` defines the `store` fixture (the SQLite one from `test_store.py`, now `SqliteStore.open(tmp_path / "test.db")`), `ATTEMPTS` and `degrade()`.
  - Into `tests/store/test_contract.py`, move every test from `tests/test_store.py` **except**:
    - `test_recover_dead_letters_turns_interrupted_by_a_crash` (it reopens a file);
    - `test_a_failed_commit_does_not_leave_the_connection_in_a_transaction`;
    - `test_a_database_from_the_first_schema_version_is_migrated`.

    Those three move to `tests/store/test_sqlite.py` and keep using `SqliteStore` directly.
  - Add this to `test_contract.py`, so recovery is part of the contract:

```python
async def test_recover_without_lease_dead_letters_every_in_progress_turn(store: Store) -> None:
    await store.begin_turn("c1", "t1", {"audio_b64": "AAAA"})
    ids = await store.recover()
    assert len(ids) == 1
    turn = await store.get_turn("c1", "t1")
    assert turn is not None and turn.status is TurnStatus.INTERRUPTED
    entry = await store.get_dead_letter(ids[0])
    assert entry is not None and entry.reason == "interrupted" and entry.status == "pending"
```

  - In `tests/test_cli.py`, replace `Store.open` with `SqliteStore.open` (import from `voice_agent.store`).

- [ ] **Step 7: Run everything.** `uv run pytest -q`: same count as before + 1, all pass. Then run `uv run mypy` and `uv run ruff check . && uv run ruff format --check .`.

- [ ] **Step 8: Commit.**
```bash
git add -A src/voice_agent/store src/voice_agent/store.py tests/store tests/test_store.py tests/test_cli.py src/voice_agent/wiring.py src/voice_agent/cli.py
git commit -m "refactor: store becomes a package with a Store protocol; SQLite backend unchanged"
```

---

### Task 2: DynamoDB backend, shared-store recovery, settings

**Files:**
- Create: `src/voice_agent/store/dynamodb.py`, `tests/store/test_dynamodb.py`
- Modify:
  - `pyproject.toml`: add `boto3>=1.35` to dependencies, `moto[dynamodb]>=5.0` to dev, and a mypy override;
  - `src/voice_agent/config.py`: store settings;
  - `src/voice_agent/store/__init__.py`: `open_store` dispatch;
  - `src/voice_agent/wiring.py`: lease-bounded startup recovery for shared stores;
  - `tests/store/conftest.py`: parametrise `store` over both backends;
  - `.env.example`.

**Interfaces:**
- Consumes: the `Store` protocol and records (Task 1).
- Produces:
  - `DynamoStore(client, table)` with `shared = True` and the classmethod `open(table, *, region, endpoint_url=None) -> DynamoStore`;
  - `TABLE_SCHEMA: dict[str, Any]` and `STATUS_INDEX = "by_status"`; Terraform (Task 7) mirrors these exactly;
  - the Settings fields `store_backend: Literal["sqlite","dynamodb"] = "sqlite"`, `dynamodb_table: str = "collectionsinference-state"`, `aws_region: str = "ap-south-1"` and `dynamodb_endpoint_url: str | None = None`;
  - `voice_agent.wiring.lease_seconds(settings) -> float`.

Item layout (single table, string keys `pk`/`sk`, sparse GSI `by_status` on `gsi1pk`/`gsi1sk`):

| Item | pk | sk | gsi1pk / gsi1sk |
|---|---|---|---|
| turn | `CALL#{call_id}` | `TURN#{turn_id}` | `TURN#in_progress` / `updated_at`, only while in progress |
| dead letter | `DLQ#{id:012d}` | `DLQ` | `DLQ#{status}` / `{id:012d}` |
| id counter | `COUNTER` | `DLQ` | none |

JSON payloads are stored as strings. A turn's `arrival` attribute (zero-padded ns + process counter) gives arrival order.

- [ ] **Step 1: Dependencies.** Add `"boto3>=1.35"` to `[project].dependencies` and `"moto[dynamodb]>=5.0"` to `[dependency-groups].dev`. Append to `pyproject.toml`:

```toml
[[tool.mypy.overrides]]
module = ["boto3", "boto3.*", "botocore", "botocore.*", "moto"]
ignore_missing_imports = true
```

Run `uv lock && uv sync`.

- [ ] **Step 2: Parametrise the contract fixture** in `tests/store/conftest.py`:

```python
@pytest.fixture(params=["sqlite", "dynamodb"])
async def store(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Store]:
    if request.param == "sqlite":
        sqlite_store = await SqliteStore.open(tmp_path / "test.db")
        yield sqlite_store
        await sqlite_store.close()
        return
    for var in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    with mock_aws():
        client = boto3.client("dynamodb", region_name="ap-south-1")
        client.create_table(TableName="test-state", **TABLE_SCHEMA)
        dynamo_store = DynamoStore(client, "test-state")
        yield dynamo_store
        await dynamo_store.close()
```

Imports: `boto3`, `from moto import mock_aws`, `from voice_agent.store.dynamodb import TABLE_SCHEMA, DynamoStore`.

- [ ] **Step 3: Run the contract tests and watch the DynamoDB half fail.** `uv run pytest tests/store -q`. Expected: `ModuleNotFoundError: voice_agent.store.dynamodb`.

- [ ] **Step 4: Write `src/voice_agent/store/dynamodb.py`:**

```python
"""DynamoDB persistence: the same contract as the SQLite store, for many tasks at once.

One table holds turns, dead letters and the dead-letter id counter:

* A turn is written ahead (`in_progress`) with a create-only condition, so a duplicate
  turn id is rejected exactly as SQLite's primary key rejects it.
* "Turn degraded + dead letter written" is one TransactWriteItems call. The turn update
  is conditional on `status = in_progress`, so a turn can never be dead-lettered twice
  and never marked degraded without its dead letter (or vice versa).
* Every transition is a conditional update on the expected status, mirroring the SQL
  `WHERE status = <expected>` guards.
* A sparse index (`by_status`) lists dead letters by status and finds in-progress turns
  for the lease sweeper; items leave it when they no longer need finding.

boto3 is synchronous, so each call runs in a worker thread; multi-step writes are
shielded from cancellation like the SQLite store's.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, TypeVar, cast

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.config import Config
from botocore.exceptions import ClientError

from voice_agent.store.records import (
    DEAD_LETTER_STATUSES,
    LIVE_DEGRADED,
    DeadLetter,
    Turn,
    TurnStatus,
    dumps,
    iso,
    loads,
    now,
)

T = TypeVar("T")

STATUS_INDEX = "by_status"
IN_PROGRESS_PK = "TURN#in_progress"

TABLE_SCHEMA: dict[str, Any] = {
    "AttributeDefinitions": [
        {"AttributeName": name, "AttributeType": "S"} for name in ("pk", "sk", "gsi1pk", "gsi1sk")
    ],
    "KeySchema": [
        {"AttributeName": "pk", "KeyType": "HASH"},
        {"AttributeName": "sk", "KeyType": "RANGE"},
    ],
    "GlobalSecondaryIndexes": [
        {
            "IndexName": STATUS_INDEX,
            "KeySchema": [
                {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }
    ],
    "BillingMode": "PAY_PER_REQUEST",
}

_serializer = TypeSerializer()
_deserializer = TypeDeserializer()
_sequence = itertools.count()


def _turn_key(call_id: str, turn_id: str) -> dict[str, Any]:
    return {"pk": {"S": f"CALL#{call_id}"}, "sk": {"S": f"TURN#{turn_id}"}}


def _dlq_key(dlq_id: int) -> dict[str, Any]:
    return {"pk": {"S": f"DLQ#{dlq_id:012d}"}, "sk": {"S": "DLQ"}}


def _arrival() -> str:
    """Sortable arrival stamp: wall-clock ns plus a process counter for ties."""
    return f"{time.time_ns():020d}{next(_sequence) % 1_000_000:06d}"


def _encode(item: dict[str, Any]) -> dict[str, Any]:
    return {k: _serializer.serialize(v) for k, v in item.items() if v is not None}


def _decode(item: dict[str, Any]) -> dict[str, Any]:
    decoded = {k: _deserializer.deserialize(v) for k, v in item.items()}
    return {k: int(v) if isinstance(v, Decimal) else v for k, v in decoded.items()}


def _update(
    fields: dict[str, Any],
    *,
    remove: tuple[str, ...] = (),
    condition: dict[str, Any] | None = None,
    increment: tuple[str, ...] = (),
) -> dict[str, Any]:
    """UpdateItem arguments with every attribute name aliased (several are reserved
    words). `condition` maps attribute -> required current value."""
    names = {f"#{k}": k for k in (*fields, *remove, *increment, *(condition or {}))}
    values = {f":{k}": _serializer.serialize(v) for k, v in fields.items()}
    sets = [f"#{k} = :{k}" for k in fields]
    if increment:
        values[":one"] = {"N": "1"}
        sets += [f"#{k} = #{k} + :one" for k in increment]
    expression = "SET " + ", ".join(sets)
    if remove:
        expression += " REMOVE " + ", ".join(f"#{k}" for k in remove)
    args: dict[str, Any] = {"UpdateExpression": expression, "ExpressionAttributeNames": names}
    if condition:
        args["ConditionExpression"] = " AND ".join(f"#{k} = :expected_{k}" for k in condition)
        values.update({f":expected_{k}": _serializer.serialize(v) for k, v in condition.items()})
    args["ExpressionAttributeValues"] = values
    return args


def _condition_failed(exc: ClientError) -> bool:
    code = exc.response.get("Error", {}).get("Code", "")
    if code == "ConditionalCheckFailedException":
        return True
    if code == "TransactionCanceledException":
        reasons = exc.response.get("CancellationReasons") or []
        if any(r.get("Code") == "ConditionalCheckFailed" for r in reasons):
            return True
        return "ConditionalCheckFailed" in str(exc)
    return False


def _turn(d: dict[str, Any]) -> Turn:
    return Turn(
        call_id=d["call_id"],
        turn_id=d["turn_id"],
        status=TurnStatus(d["status"]),
        action=d.get("action"),
        request=loads(d["request"]),
        result=loads(d.get("result")),
        failed_stage=d.get("failed_stage"),
        error_kind=d.get("error_kind"),
        attempts=loads(d.get("attempts", "[]")),
        created_at=d["created_at"],
        updated_at=d["updated_at"],
    )


def _dead_letter(d: dict[str, Any]) -> DeadLetter:
    return DeadLetter(
        id=d["id"],
        call_id=d["call_id"],
        turn_id=d["turn_id"],
        reason=d["reason"],
        failed_stage=d.get("failed_stage"),
        error_kind=d.get("error_kind"),
        error_detail=d.get("error_detail"),
        payload=loads(d["payload"]),
        attempts=loads(d["attempts"]),
        partial=loads(d["partial"]),
        status=d["status"],
        replay_count=d.get("replay_count", 0),
        last_replay_error=d.get("last_replay_error"),
        replay_attempts=loads(d.get("replay_attempts")),
        created_at=d["created_at"],
        claimed_at=d.get("claimed_at"),
        last_replay_at=d.get("last_replay_at"),
        resolved_at=d.get("resolved_at"),
    )


class DynamoStore:
    shared = True

    def __init__(self, client: Any, table: str) -> None:
        self._client = client
        self._table = table

    @classmethod
    def open(cls, table: str, *, region: str, endpoint_url: str | None = None) -> DynamoStore:
        client = boto3.client(
            "dynamodb",
            region_name=region,
            endpoint_url=endpoint_url,
            config=Config(
                retries={"mode": "standard", "max_attempts": 4},
                connect_timeout=2,
                read_timeout=5,
            ),
        )
        return cls(client, table)

    async def close(self) -> None:
        await asyncio.to_thread(self._client.close)

    # -- plumbing -------------------------------------------------------------

    async def _call(self, operation: str, **kwargs: Any) -> dict[str, Any]:
        method = getattr(self._client, operation)
        return cast(dict[str, Any], await asyncio.to_thread(method, **kwargs))

    async def _write(self, work: Callable[[], Awaitable[T]]) -> T:
        """Once started, a multi-step write runs to completion even if the caller is
        cancelled (same guarantee as the SQLite store)."""
        return await asyncio.shield(work())

    async def _query(self, **kwargs: Any) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        limit = kwargs.pop("limit", None)
        start = None
        while True:
            page_args = dict(kwargs, TableName=self._table)
            if start:
                page_args["ExclusiveStartKey"] = start
            page = await self._call("query", **page_args)
            items.extend(_decode(i) for i in page.get("Items", []))
            start = page.get("LastEvaluatedKey")
            if not start or (limit is not None and len(items) >= limit):
                return items if limit is None else items[:limit]

    async def _index(
        self, pk: str, *, before: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        condition = "gsi1pk = :pk" + (" AND gsi1sk < :before" if before is not None else "")
        values: dict[str, Any] = {":pk": {"S": pk}}
        if before is not None:
            values[":before"] = {"S": before}
        return await self._query(
            IndexName=STATUS_INDEX,
            KeyConditionExpression=condition,
            ExpressionAttributeValues=values,
            limit=limit,
        )

    async def _call_turns(self, call_id: str) -> list[dict[str, Any]]:
        items = await self._query(
            KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={":pk": {"S": f"CALL#{call_id}"}, ":prefix": {"S": "TURN#"}},
            ConsistentRead=True,
        )
        return sorted(items, key=lambda d: str(d["arrival"]))

    async def _next_dead_letter_id(self) -> int:
        response = await self._call(
            "update_item",
            TableName=self._table,
            Key={"pk": {"S": "COUNTER"}, "sk": {"S": "DLQ"}},
            UpdateExpression="ADD #n :one",
            ExpressionAttributeNames={"#n": "n"},
            ExpressionAttributeValues={":one": {"N": "1"}},
            ReturnValues="UPDATED_NEW",
        )
        return int(response["Attributes"]["n"]["N"])

    # -- turns ----------------------------------------------------------------

    async def begin_turn(self, call_id: str, turn_id: str, request: dict[str, Any]) -> bool:
        stamp = now()
        item = {
            **_turn_key(call_id, turn_id),
            **_encode(
                {
                    "call_id": call_id,
                    "turn_id": turn_id,
                    "arrival": _arrival(),
                    "status": TurnStatus.IN_PROGRESS.value,
                    "request": dumps(request),
                    "attempts": "[]",
                    "created_at": stamp,
                    "updated_at": stamp,
                    "gsi1pk": IN_PROGRESS_PK,
                    "gsi1sk": stamp,
                }
            ),
        }

        async def work() -> bool:
            try:
                await self._call(
                    "put_item",
                    TableName=self._table,
                    Item=item,
                    ConditionExpression="attribute_not_exists(pk)",
                )
            except ClientError as exc:
                if _condition_failed(exc):
                    return False
                raise
            return True

        return await self._write(work)

    async def get_turn(self, call_id: str, turn_id: str) -> Turn | None:
        response = await self._call(
            "get_item", TableName=self._table, Key=_turn_key(call_id, turn_id), ConsistentRead=True
        )
        item = response.get("Item")
        return _turn(_decode(item)) if item else None

    async def list_turns(self, call_id: str) -> list[Turn]:
        return [_turn(d) for d in await self._call_turns(call_id)]

    async def history_before(
        self, call_id: str, turn_id: str, *, limit: int
    ) -> list[tuple[str, str]]:
        if limit <= 0:
            return []
        turns = await self._call_turns(call_id)
        ids = [t["turn_id"] for t in turns]
        if turn_id not in ids:
            return []
        earlier = [t for t in turns[: ids.index(turn_id)] if t["status"] == TurnStatus.COMPLETED]
        history = []
        for t in earlier[-limit:]:
            result = loads(t.get("result")) or {}
            history.append((str(result.get("transcript", "")), str(result.get("reply_text", ""))))
        return history

    async def complete_turn(
        self,
        call_id: str,
        turn_id: str,
        result: dict[str, Any],
        attempts: list[dict[str, Any]],
    ) -> bool:
        args = _update(
            {
                "status": TurnStatus.COMPLETED.value,
                "action": "continue",
                "result": dumps(result),
                "attempts": dumps(attempts),
                "updated_at": now(),
            },
            remove=("gsi1pk", "gsi1sk"),
            condition={"status": TurnStatus.IN_PROGRESS.value},
        )

        async def work() -> bool:
            try:
                await self._call(
                    "update_item", TableName=self._table, Key=_turn_key(call_id, turn_id), **args
                )
            except ClientError as exc:
                if _condition_failed(exc):
                    return False
                raise
            return True

        return await self._write(work)

    async def degrade_turn(
        self,
        call_id: str,
        turn_id: str,
        *,
        failed_stage: str,
        error_kind: str,
        error_detail: str,
        attempts: list[dict[str, Any]],
        partial: dict[str, Any],
        action: str | None = None,
    ) -> int:
        return await self._write(
            lambda: self._dead_letter(
                call_id,
                turn_id,
                status=TurnStatus.DEGRADED,
                reason="stage_failed",
                failed_stage=failed_stage,
                error_kind=error_kind,
                error_detail=error_detail,
                attempts=attempts,
                partial=partial,
                action=action,
            )
        )

    async def _dead_letter(
        self,
        call_id: str,
        turn_id: str,
        *,
        status: TurnStatus,
        reason: str,
        failed_stage: str | None,
        error_kind: str | None,
        error_detail: str | None,
        attempts: list[dict[str, Any]],
        partial: dict[str, Any],
        action: str | None,
    ) -> int:
        current = await self.get_turn(call_id, turn_id)
        if current is None or current.status is not TurnStatus.IN_PROGRESS:
            raise LookupError(f"turn {call_id}/{turn_id} is not in progress")
        dlq_id = await self._next_dead_letter_id()
        stamp = now()
        turn_update = _update(
            {
                "status": status.value,
                "action": action,
                "failed_stage": failed_stage,
                "error_kind": error_kind,
                "attempts": dumps(attempts),
                "result": dumps(partial),
                "updated_at": stamp,
            },
            remove=("gsi1pk", "gsi1sk"),
            condition={"status": TurnStatus.IN_PROGRESS.value},
        )
        entry = {
            **_dlq_key(dlq_id),
            **_encode(
                {
                    "id": dlq_id,
                    "call_id": call_id,
                    "turn_id": turn_id,
                    "reason": reason,
                    "failed_stage": failed_stage,
                    "error_kind": error_kind,
                    "error_detail": error_detail,
                    "payload": dumps(current.request),
                    "attempts": dumps(attempts),
                    "partial": dumps(partial),
                    "status": "pending",
                    "replay_count": 0,
                    "created_at": stamp,
                    "gsi1pk": "DLQ#pending",
                    "gsi1sk": f"{dlq_id:012d}",
                }
            ),
        }
        try:
            await self._call(
                "transact_write_items",
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self._table,
                            "Key": _turn_key(call_id, turn_id),
                            **turn_update,
                        }
                    },
                    {
                        "Put": {
                            "TableName": self._table,
                            "Item": entry,
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                ],
            )
        except ClientError as exc:
            if _condition_failed(exc):
                raise LookupError(f"turn {call_id}/{turn_id} is not in progress") from exc
            raise
        return dlq_id

    async def consecutive_degraded(self, call_id: str) -> int:
        finished = [
            t
            for t in reversed(await self._call_turns(call_id))
            if t["status"] != TurnStatus.IN_PROGRESS
        ][:50]
        count = 0
        for t in finished:
            if t["status"] not in LIVE_DEGRADED:
                break
            count += 1
        return count

    async def recover(self, *, stale_after_s: float | None = None) -> list[int]:
        """Same contract as the SQLite store. Safe with many tasks: a turn finished or
        reclaimed by another task since the index was read is skipped (LookupError)."""
        cutoff = (
            iso(datetime.now(UTC) - timedelta(seconds=stale_after_s))
            if stale_after_s is not None
            else "9999"
        )
        detail = (
            "service stopped while the turn was in progress"
            if stale_after_s is None
            else f"turn did not finish within {stale_after_s:g}s"
        )

        async def work() -> list[int]:
            for entry in await self._index("DLQ#replaying"):
                if (entry.get("claimed_at") or "") < cutoff:
                    await self._set_dead_letter_status(entry["id"], "replaying", "pending")
            ids = []
            for turn in await self._index(IN_PROGRESS_PK, before=cutoff):
                try:
                    ids.append(
                        await self._dead_letter(
                            turn["call_id"],
                            turn["turn_id"],
                            status=TurnStatus.INTERRUPTED,
                            reason="interrupted",
                            failed_stage=None,
                            error_kind=None,
                            error_detail=detail,
                            attempts=[],
                            partial={},
                            action=None,
                        )
                    )
                except LookupError:
                    continue
            return ids

        return await self._write(work)

    # -- dead letters ---------------------------------------------------------

    async def _set_dead_letter_status(self, dlq_id: int, expected: str, new: str) -> bool:
        args = _update({"status": new, "gsi1pk": f"DLQ#{new}"}, condition={"status": expected})
        try:
            await self._call("update_item", TableName=self._table, Key=_dlq_key(dlq_id), **args)
        except ClientError as exc:
            if _condition_failed(exc):
                return False
            raise
        return True

    async def get_dead_letter(self, dlq_id: int) -> DeadLetter | None:
        response = await self._call(
            "get_item", TableName=self._table, Key=_dlq_key(dlq_id), ConsistentRead=True
        )
        item = response.get("Item")
        return _dead_letter(_decode(item)) if item else None

    async def list_dead_letters(
        self, *, status: str | None = None, call_id: str | None = None, limit: int = 100
    ) -> list[DeadLetter]:
        statuses = [status] if status is not None else list(DEAD_LETTER_STATUSES)
        entries: list[dict[str, Any]] = []
        for s in statuses:
            found = await self._index(f"DLQ#{s}", limit=None if call_id else limit)
            entries.extend(e for e in found if call_id is None or e["call_id"] == call_id)
        entries.sort(key=lambda e: int(e["id"]))
        return [_dead_letter(e) for e in entries[:limit]]

    async def dead_letter_counts(self) -> dict[str, int]:
        counts = dict.fromkeys(DEAD_LETTER_STATUSES, 0)
        for s in DEAD_LETTER_STATUSES:
            start = None
            while True:
                args: dict[str, Any] = {
                    "TableName": self._table,
                    "IndexName": STATUS_INDEX,
                    "KeyConditionExpression": "gsi1pk = :pk",
                    "ExpressionAttributeValues": {":pk": {"S": f"DLQ#{s}"}},
                    "Select": "COUNT",
                }
                if start:
                    args["ExclusiveStartKey"] = start
                page = await self._call("query", **args)
                counts[s] += int(page.get("Count", 0))
                start = page.get("LastEvaluatedKey")
                if not start:
                    break
        return counts

    async def claim_dead_letter(self, dlq_id: int) -> DeadLetter | None:
        args = _update(
            {"status": "replaying", "gsi1pk": "DLQ#replaying", "claimed_at": now()},
            condition={"status": "pending"},
        )

        async def work() -> DeadLetter | None:
            try:
                response = await self._call(
                    "update_item",
                    TableName=self._table,
                    Key=_dlq_key(dlq_id),
                    ReturnValues="ALL_NEW",
                    **args,
                )
            except ClientError as exc:
                if _condition_failed(exc):
                    return None
                raise
            return _dead_letter(_decode(response["Attributes"]))

        return await self._write(work)

    async def release_dead_letter(
        self, dlq_id: int, *, error: str, attempts: list[dict[str, Any]]
    ) -> bool:
        args = _update(
            {
                "status": "pending",
                "gsi1pk": "DLQ#pending",
                "last_replay_error": error,
                "replay_attempts": dumps(attempts),
                "last_replay_at": now(),
            },
            increment=("replay_count",),
            condition={"status": "replaying"},
        )

        async def work() -> bool:
            try:
                await self._call("update_item", TableName=self._table, Key=_dlq_key(dlq_id), **args)
            except ClientError as exc:
                if _condition_failed(exc):
                    return False
                raise
            return True

        return await self._write(work)

    async def resolve_dead_letter(
        self, dlq_id: int, result: dict[str, Any], attempts: list[dict[str, Any]]
    ) -> bool:
        async def work() -> bool:
            entry = await self.get_dead_letter(dlq_id)
            if entry is None or entry.status != "replaying":
                return False
            stamp = now()
            resolve = _update(
                {
                    "status": "resolved",
                    "gsi1pk": "DLQ#resolved",
                    "replay_attempts": dumps(attempts),
                    "last_replay_at": stamp,
                    "resolved_at": stamp,
                },
                remove=("last_replay_error",),
                increment=("replay_count",),
                condition={"status": "replaying"},
            )
            complete = _update(
                {
                    "status": TurnStatus.COMPLETED_ON_REPLAY.value,
                    "result": dumps(result),
                    "updated_at": stamp,
                }
            )
            try:
                await self._call(
                    "transact_write_items",
                    TransactItems=[
                        {"Update": {"TableName": self._table, "Key": _dlq_key(dlq_id), **resolve}},
                        {
                            "Update": {
                                "TableName": self._table,
                                "Key": _turn_key(entry.call_id, entry.turn_id),
                                **complete,
                            }
                        },
                    ],
                )
            except ClientError as exc:
                if _condition_failed(exc):
                    return False
                raise
            return True

        return await self._write(work)
```

- [ ] **Step 5: Run the contract tests on both backends.** `uv run pytest tests/store -q`. Expected: every contract test passes twice (`[sqlite]` and `[dynamodb]`).
  - If botocore or moto raise `DeprecationWarning` under `filterwarnings = error`, add narrowly scoped ignores to `[tool.pytest.ini_options].filterwarnings`, e.g. `"ignore::DeprecationWarning:botocore.*"`. Never ignore warnings globally.
  - Fix any real contract difference in `dynamodb.py`, never in the tests.

- [ ] **Step 6: Add DynamoDB-specific tests** in `tests/store/test_dynamodb.py`. Reuse the conftest's moto setup through a local fixture `dynamo`, which yields `(client, DynamoStore)`. Include:

```python
async def test_dead_letter_ids_are_unique_and_increasing(dynamo) -> None:
    _, store = dynamo
    ids = []
    for n in range(3):
        await store.begin_turn("c1", f"t{n}", {"audio_b64": "AAAA"})
        ids.append(
            await store.degrade_turn(
                "c1",
                f"t{n}",
                failed_stage="llm",
                error_kind="server_error",
                error_detail="x",
                attempts=[],
                partial={},
            )
        )
    assert ids == sorted(ids) and len(set(ids)) == 3


async def test_a_turn_is_never_dead_lettered_twice(dynamo) -> None:
    _, store = dynamo
    await store.begin_turn("c1", "t1", {"audio_b64": "AAAA"})
    await store.degrade_turn(
        "c1",
        "t1",
        failed_stage="llm",
        error_kind="server_error",
        error_detail="x",
        attempts=[],
        partial={},
    )
    with pytest.raises(LookupError):
        await store.degrade_turn(
            "c1",
            "t1",
            failed_stage="llm",
            error_kind="server_error",
            error_detail="x",
            attempts=[],
            partial={},
        )
    assert (await store.dead_letter_counts())["pending"] == 1


async def test_finished_turns_leave_the_status_index(dynamo) -> None:
    client, store = dynamo
    await store.begin_turn("c1", "t1", {"audio_b64": "AAAA"})
    await store.complete_turn("c1", "t1", {"transcript": "a", "reply_text": "b"}, [])
    page = client.query(
        TableName="test-state",
        IndexName="by_status",
        KeyConditionExpression="gsi1pk = :pk",
        ExpressionAttributeValues={":pk": {"S": "TURN#in_progress"}},
    )
    assert page["Items"] == []


async def test_list_dead_letters_pages_past_one_query_page(dynamo) -> None:
    _, store = dynamo
    for n in range(120):
        await store.begin_turn("c1", f"t{n}", {"audio_b64": "AAAA"})
        await store.degrade_turn(
            "c1",
            f"t{n}",
            failed_stage="llm",
            error_kind="server_error",
            error_detail="x",
            attempts=[],
            partial={},
        )
    assert len(await store.list_dead_letters(limit=1000)) == 120
    assert (await store.dead_letter_counts())["pending"] == 120
```

  Run: `uv run pytest tests/store -q`. Expected: PASS.

- [ ] **Step 7: Settings and `open_store`.** In `config.py`, add after `log_level`:

```python
    # Persistence: SQLite for one process, DynamoDB when several tasks share state.
    store_backend: Literal["sqlite", "dynamodb"] = "sqlite"
    dynamodb_table: str = "collectionsinference-state"
    aws_region: str = "ap-south-1"
    dynamodb_endpoint_url: str | None = None
```

In `store/__init__.py`, `open_store` becomes:

```python
async def open_store(settings: Settings) -> Store:
    """The backend `settings.store_backend` names."""
    if settings.store_backend == "dynamodb":
        from voice_agent.store.dynamodb import DynamoStore  # boto3 only when needed

        return DynamoStore.open(
            settings.dynamodb_table,
            region=settings.aws_region,
            endpoint_url=settings.dynamodb_endpoint_url,
        )
    return await SqliteStore.open(settings.db_path)
```

- [ ] **Step 8: Write a failing test for shared-store startup recovery** in `tests/test_wiring.py`:

```python
from pathlib import Path

from tests.fakes import fake_providers, fast_settings
from voice_agent.store import SqliteStore
from voice_agent.wiring import lease_seconds, open_runtime


async def test_shared_store_startup_recovery_spares_other_tasks_live_turns(
    tmp_path: Path, monkeypatch
) -> None:
    settings = fast_settings(tmp_path)
    seed = await SqliteStore.open(settings.db_path)
    await seed.begin_turn("c1", "t1", {"audio_b64": "AAAA"})  # another task's live turn
    await seed.close()
    monkeypatch.setattr(SqliteStore, "shared", True)  # behave like DynamoDB
    providers, *_ = fake_providers()
    runtime = await open_runtime(settings, providers=providers)
    try:
        assert runtime.recovered_dead_letters == []
        turn = await runtime.store.get_turn("c1", "t1")
        assert turn is not None and turn.status.value == "in_progress"
    finally:
        await runtime.close()


def test_lease_is_at_least_a_minute_and_three_deadlines(tmp_path: Path) -> None:
    assert lease_seconds(fast_settings(tmp_path, turn_deadline_s=6)) == 60
    assert lease_seconds(fast_settings(tmp_path, turn_deadline_s=30)) == 90
```

Run `uv run pytest tests/test_wiring.py -q`. Expected: FAIL (`lease_seconds` is not defined).

- [ ] **Step 9: Implement it** in `wiring.py`:

```python
def lease_seconds(settings: Settings) -> float:
    """How long a turn or a replay claim may sit untouched before it is reclaimed:
    comfortably longer than any turn can legitimately run."""
    return max(60.0, 3 * settings.turn_deadline_s)
```

  - `Runtime.lease_s` returns `lease_seconds(self.settings)`.
  - In `_wire`, replace the recovery line with:

```python
    # A shared store (DynamoDB) is used by other live tasks: at startup reclaim only
    # work that has outlived the lease, never another task's in-flight turns.
    stale_after = lease_seconds(settings) if store.shared else None
    recovered = await store.recover(stale_after_s=stale_after) if recover else []
```

  Run `uv run pytest -q`. Expected: all pass.

- [ ] **Step 10: Update `.env.example`.** Add a block:

```
# Persistence: sqlite (default) or dynamodb (AWS; credentials from the standard AWS chain)
STORE_BACKEND=sqlite
# DYNAMODB_TABLE=collectionsinference-state
# AWS_REGION=ap-south-1
```

- [ ] **Step 11: Lint, type-check and commit.**
```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q
git add -A && git commit -m "feat: DynamoDB store backend with transactional dead-lettering; shared stores recover by lease only"
```

---

### Task 3: Active-call tracking, end-call endpoint, EMF metrics

**Files:**
- Create: `src/voice_agent/calls.py`, `src/voice_agent/emf.py`, `tests/test_calls.py`, `tests/test_emf.py`
- Modify: `src/voice_agent/metrics.py`, `src/voice_agent/pipeline.py` (lines 134, 188–189, 287, 291, 303), `src/voice_agent/dlq.py:186`, `src/voice_agent/wiring.py`, `src/voice_agent/api.py`, `src/voice_agent/config.py`, `tests/test_api.py`

**Interfaces:**
- Produces:
  - `CallTracker(idle_s: float, clock=time.monotonic)`, with `.touch(call_id)`, `.end(call_id) -> bool`, `.active() -> int`, `.in_flight: int` and the context manager `.turn(call_id)`;
  - `EmfReporter(namespace: str, service: str, clock=time.time)`, with `.turn(status, seconds | None)`, `.attempt(stage, outcome, retry: bool)`, `.breaker(stage, value: int)`, `.dead_letter()`, `.replay(status)` and `.flush(*, active_calls, in_flight, dlq_pending) -> list[dict]`;
  - `Metrics(emf: EmfReporter | None = None)`, with the new methods `observe_turn(status, seconds | None)`, `observe_dead_letter(reason)`, `observe_replay(status)` and gauges `active_calls` and `in_flight_turns`;
  - `Runtime.calls`, `Runtime.emf`, `Runtime.run_emf()`;
  - `POST /v1/calls/{call_id}/end` returning `{"call_id", "was_active"}`;
  - Settings `emf_enabled: bool = False`, `emf_namespace = "CollectionsChallenge"`, `emf_service_name = "voice-agent"`, `emf_interval_s = 10.0`, `call_idle_s = 45.0`.
- EMF metric names (the dashboard, alarms and autoscaling in Tasks 8 and 10 use these exact names):
  - dimension `ServiceName`: `ActiveCalls`, `InFlightTurns`, `Turns`, `TurnsCompleted`, `TurnsDegraded`, `TurnLatency` (Milliseconds), `DeadLetters`, `DlqPending`, `Replays`, `ReplaysResolved`;
  - dimensions `ServiceName, Stage`: `ProviderAttempts`, `ProviderFailures`, `Retries`, `BreakerState`.

- [ ] **Step 1: Write failing tests for `CallTracker`** (`tests/test_calls.py`):

```python
from voice_agent.calls import CallTracker


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_a_call_is_active_until_ended() -> None:
    tracker = CallTracker(idle_s=45, clock=Clock())
    tracker.touch("a")
    tracker.touch("b")
    assert tracker.active() == 2
    assert tracker.end("a") is True
    assert tracker.end("a") is False
    assert tracker.active() == 1


def test_an_idle_call_stops_counting_after_idle_s() -> None:
    clock = Clock()
    tracker = CallTracker(idle_s=45, clock=clock)
    tracker.touch("a")
    clock.now = 44.9
    assert tracker.active() == 1
    clock.now = 45.1
    assert tracker.active() == 0


def test_turn_counts_in_flight_and_refreshes_the_call() -> None:
    clock = Clock()
    tracker = CallTracker(idle_s=45, clock=clock)
    with tracker.turn("a"):
        assert tracker.in_flight == 1
        clock.now = 100
    assert tracker.in_flight == 0
    clock.now = 140
    assert tracker.active() == 1  # touched again when the turn finished at t=100
```

Run `uv run pytest tests/test_calls.py -q`. Expected: FAIL (no module).

- [ ] **Step 2: Implement `calls.py`:**

```python
"""Which calls this task is currently holding.

With ALB stickiness every turn of a call lands on the same task, so "active calls on
this task" is the load signal autoscaling targets: a call occupies capacity between
turns too, not just while a turn is being processed. A call stops counting when the
client ends it (POST /v1/calls/{id}/end) or after `idle_s` without a turn.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager


class CallTracker:
    def __init__(self, idle_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._idle_s = idle_s
        self._clock = clock
        self._last_seen: dict[str, float] = {}
        self.in_flight = 0

    def touch(self, call_id: str) -> None:
        self._last_seen[call_id] = self._clock()

    def end(self, call_id: str) -> bool:
        return self._last_seen.pop(call_id, None) is not None

    def active(self) -> int:
        cutoff = self._clock() - self._idle_s
        for call_id in [c for c, seen in self._last_seen.items() if seen < cutoff]:
            del self._last_seen[call_id]
        return len(self._last_seen)

    @contextmanager
    def turn(self, call_id: str) -> Iterator[None]:
        self.touch(call_id)
        self.in_flight += 1
        try:
            yield
        finally:
            self.in_flight -= 1
            self.touch(call_id)
```

Run `uv run pytest tests/test_calls.py -q`. Expected: PASS.

- [ ] **Step 3: Write failing tests for EMF** (`tests/test_emf.py`):

```python
from voice_agent.emf import EmfReporter


def reporter() -> EmfReporter:
    return EmfReporter(
        namespace="CollectionsChallenge", service="voice-agent", clock=lambda: 1700000000.0
    )


def service_doc(docs: list[dict]) -> dict:
    return next(d for d in docs if "Stage" not in d)


def test_flush_reports_active_calls_every_time_even_when_idle() -> None:
    docs = reporter().flush(active_calls=7, in_flight=2, dlq_pending=None)
    doc = service_doc(docs)
    meta = doc["_aws"]["CloudWatchMetrics"][0]
    assert meta["Namespace"] == "CollectionsChallenge"
    assert meta["Dimensions"] == [["ServiceName"]]
    assert doc["ServiceName"] == "voice-agent"
    assert doc["ActiveCalls"] == 7 and doc["InFlightTurns"] == 2
    assert doc["_aws"]["Timestamp"] == 1700000000000
    assert "DlqPending" not in doc


def test_turns_and_latency_are_reported_then_reset() -> None:
    r = reporter()
    r.turn("completed", 0.5)
    r.turn("degraded", 1.25)
    r.turn("unrecorded", None)
    doc = service_doc(r.flush(active_calls=0, in_flight=0, dlq_pending=3))
    assert doc["Turns"] == 3 and doc["TurnsCompleted"] == 1 and doc["TurnsDegraded"] == 2
    assert doc["TurnLatency"] == [500.0, 1250.0]
    assert doc["DlqPending"] == 3
    names = {m["Name"]: m.get("Unit") for m in doc["_aws"]["CloudWatchMetrics"][0]["Metrics"]}
    assert names["TurnLatency"] == "Milliseconds"
    again = service_doc(r.flush(active_calls=0, in_flight=0, dlq_pending=None))
    assert again["Turns"] == 0 and "TurnLatency" not in again


def test_latency_values_are_split_into_documents_of_at_most_100() -> None:
    r = reporter()
    for _ in range(250):
        r.turn("completed", 0.1)
    docs = [d for d in r.flush(active_calls=0, in_flight=0, dlq_pending=None) if "TurnLatency" in d]
    assert [len(d["TurnLatency"]) for d in docs] == [100, 100, 50]
    assert sum(d.get("Turns", 0) for d in docs) == 250  # counts reported once


def test_stage_documents_carry_attempts_failures_retries_and_breaker_state() -> None:
    r = reporter()
    r.attempt("llm", "ok", retry=False)
    r.attempt("llm", "error", retry=True)
    r.attempt("llm", "rejected", retry=True)
    r.breaker("llm", 2)
    docs = [
        d for d in r.flush(active_calls=0, in_flight=0, dlq_pending=None) if d.get("Stage") == "llm"
    ]
    assert len(docs) == 1
    doc = docs[0]
    assert doc["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["ServiceName", "Stage"]]
    assert doc["ProviderAttempts"] == 3 and doc["ProviderFailures"] == 1 and doc["Retries"] == 1
    assert doc["BreakerState"] == 2
    # breaker state is a gauge: still reported on the next flush
    nxt = [
        d for d in r.flush(active_calls=0, in_flight=0, dlq_pending=None) if d.get("Stage") == "llm"
    ][0]
    assert nxt["BreakerState"] == 2 and nxt["ProviderAttempts"] == 0
```

Run: FAIL (no module).

- [ ] **Step 4: Implement `emf.py`:**

```python
"""CloudWatch Embedded Metric Format: metrics written as structured log lines.

The awslogs driver ships stdout to CloudWatch Logs, which extracts every line carrying
an `_aws` block into metrics, so no PutMetricData permission is needed. Latencies are
sent as raw values, so CloudWatch computes true percentiles (p50/p90/p99) across tasks.

ActiveCalls is emitted on every flush with only the ServiceName dimension: averaged
over all tasks it is "calls per task", which is what target tracking scales on.
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter
from collections.abc import Callable
from typing import Any

MAX_VALUES = 100  # EMF limit on values per metric per document
STAGES = ("stt", "llm", "tts")


def write_stdout(doc: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(doc, separators=(",", ":")) + "\n")
    sys.stdout.flush()


class EmfReporter:
    def __init__(
        self, *, namespace: str, service: str, clock: Callable[[], float] = time.time
    ) -> None:
        self._namespace = namespace
        self._service = service
        self._clock = clock
        self._turns: Counter[str] = Counter()
        self._latencies_ms: list[float] = []
        self._dead_letters = 0
        self._replays: Counter[str] = Counter()
        self._attempts: dict[str, Counter[str]] = {s: Counter() for s in STAGES}
        self._breaker: dict[str, int] = dict.fromkeys(STAGES, 0)

    def turn(self, status: str, seconds: float | None) -> None:
        self._turns[status] += 1
        if seconds is not None:
            self._latencies_ms.append(round(seconds * 1000, 1))

    def attempt(self, stage: str, outcome: str, *, retry: bool) -> None:
        counts = self._attempts.setdefault(stage, Counter())
        counts["attempts"] += 1
        if outcome == "error":
            counts["failures"] += 1
        if retry:
            counts["retries"] += 1

    def breaker(self, stage: str, value: int) -> None:
        self._breaker[stage] = value

    def dead_letter(self) -> None:
        self._dead_letters += 1

    def replay(self, status: str) -> None:
        self._replays[status] += 1

    def _doc(
        self, dimensions: list[str], values: dict[str, Any], units: dict[str, str]
    ) -> dict[str, Any]:
        return {
            "_aws": {
                "Timestamp": int(self._clock() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": self._namespace,
                        "Dimensions": [dimensions],
                        "Metrics": [
                            {"Name": name, "Unit": units.get(name, "Count")} for name in values
                        ],
                    }
                ],
            },
            "ServiceName": self._service,
            **values,
        }

    def flush(
        self, *, active_calls: int, in_flight: int, dlq_pending: int | None
    ) -> list[dict[str, Any]]:
        turns = sum(self._turns.values())
        service: dict[str, Any] = {
            "ActiveCalls": active_calls,
            "InFlightTurns": in_flight,
            "Turns": turns,
            "TurnsCompleted": self._turns["completed"],
            "TurnsDegraded": turns - self._turns["completed"],
            "DeadLetters": self._dead_letters,
            "Replays": sum(self._replays.values()),
            "ReplaysResolved": self._replays["resolved"],
        }
        if dlq_pending is not None:
            service["DlqPending"] = dlq_pending
        chunks = [
            self._latencies_ms[i : i + MAX_VALUES]
            for i in range(0, len(self._latencies_ms), MAX_VALUES)
        ]
        units = {"TurnLatency": "Milliseconds"}
        docs = []
        if chunks:
            service["TurnLatency"] = chunks[0]
        docs.append(self._doc(["ServiceName"], service, units))
        docs += [self._doc(["ServiceName"], {"TurnLatency": c}, units) for c in chunks[1:]]
        for stage, counts in self._attempts.items():
            stage_doc = self._doc(
                ["ServiceName", "Stage"],
                {
                    "ProviderAttempts": counts["attempts"],
                    "ProviderFailures": counts["failures"],
                    "Retries": counts["retries"],
                    "BreakerState": self._breaker.get(stage, 0),
                },
                {},
            )
            stage_doc["Stage"] = stage
            docs.append(stage_doc)
        self._turns.clear()
        self._latencies_ms.clear()
        self._dead_letters = 0
        self._replays.clear()
        self._attempts = {s: Counter() for s in self._attempts}
        return docs
```

Run `uv run pytest tests/test_emf.py -q`. Expected: PASS.

- [ ] **Step 5: Route metric events through `Metrics`.**
  - `Metrics.__init__(self, emf: EmfReporter | None = None)` stores `self.emf`.
  - Add the gauges `self.active_calls = Gauge("active_calls", "Calls this task is holding", registry=r)` and `self.in_flight_turns = Gauge("in_flight_turns", "Turns being processed", registry=r)`.
  - Add these methods:

```python
def observe_turn(self, status: str, seconds: float | None) -> None:
    self.turns.labels(status).inc()
    if seconds is not None:
        self.turn_seconds.labels(status).observe(seconds)
    if self.emf:
        self.emf.turn(status, seconds)


def observe_dead_letter(self, reason: str) -> None:
    self.dead_letters.labels(reason).inc()
    if self.emf:
        self.emf.dead_letter()


def observe_replay(self, status: str) -> None:
    self.replays.labels(status).inc()
    if self.emf:
        self.emf.replay(status)
```

  - `observe_attempt` also calls `self.emf.attempt(stage, record.outcome, retry=record.attempt > 1 and record.outcome != "rejected")`.
  - `observe_transition` also calls `self.emf.breaker(stage, BREAKER_STATE_VALUE[new])`.

  Replace the direct uses:
  - `pipeline.py:134` becomes `self.metrics.observe_turn("unrecorded", None)`.
  - `pipeline.py:188-189` becomes `self.metrics.observe_turn(TurnStatus.COMPLETED.value, duration)`.
  - `pipeline.py:287` becomes `self.metrics.observe_dead_letter("stage_failed")`.
  - The `pipeline.py:291` increment is removed; line 303's observe becomes `self.metrics.observe_turn(TurnStatus.DEGRADED.value, duration)`, computed where `duration` is known.
  - `dlq.py:186` becomes `self._metrics.observe_replay(outcome.status)`.
  - `wiring.py` (`Runtime.sweep` and `_wire`) uses `metrics.observe_dead_letter("interrupted")`.

  Run `uv run pytest -q`. Expected: all existing tests pass; the counter values are unchanged.

- [ ] **Step 6: Settings.** Add to `config.py` after `sweep_interval_s`:

```python
    # A call counts as active until ended or idle this long (turns are ~30 s apart).
    call_idle_s: float = Field(45.0, gt=0)
    # CloudWatch Embedded Metric Format on stdout (enable on AWS).
    emf_enabled: bool = False
    emf_namespace: str = "CollectionsChallenge"
    emf_service_name: str = "voice-agent"
    emf_interval_s: float = Field(10.0, gt=0)
```

- [ ] **Step 7: Wire the runtime.**
  - `Runtime` gains `calls: CallTracker` and `emf: EmfReporter | None`.
  - In `_wire`:

```python
    emf = (
        EmfReporter(namespace=settings.emf_namespace, service=settings.emf_service_name)
        if settings.emf_enabled
        else None
    )
    metrics = Metrics(emf=emf)
```

  - Construct `calls=CallTracker(settings.call_idle_s)`.
  - Add the method:

```python
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
```

- [ ] **Step 8: API changes.** Write failing tests first in `tests/test_api.py`, using the existing app fixture pattern of that file:

```python
async def test_a_call_is_active_after_a_turn_and_not_after_end(client, runtime) -> None:
    await client.post("/v1/calls/c9/turns", json={"turn_id": "t1", "audio_b64": "AAAA"})
    assert runtime.calls.active() == 1
    r = await client.post("/v1/calls/c9/end")
    assert r.status_code == 200 and r.json() == {"call_id": "c9", "was_active": True}
    assert runtime.calls.active() == 0


async def test_metrics_exposes_active_calls(client) -> None:
    await client.post("/v1/calls/c9/turns", json={"turn_id": "t1", "audio_b64": "AAAA"})
    body = (await client.get("/metrics")).text
    assert "active_calls 1.0" in body
```

Adapt the fixture names to those already defined in `tests/test_api.py`.

Then implement:
- `post_turn` wraps the pipeline call in `with rt(request).calls.turn(call_id):`.
- New route:

```python
    @app.post("/v1/calls/{call_id}/end")
    async def end_call(call_id: str, request: Request) -> dict[str, Any]:
        return {"call_id": call_id, "was_active": rt(request).calls.end(call_id)}
```

- `/metrics` sets `metrics.active_calls.set(runtime_.calls.active())` and `metrics.in_flight_turns.set(runtime_.calls.in_flight)` before rendering.
- The lifespan starts `asyncio.create_task(opened.run_emf())` when `opened.emf is not None`, and cancels it next to the sweeper.

Run `uv run pytest -q`. Expected: PASS.

- [ ] **Step 9: Lint, type-check, run the tests and commit.**
```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q
git add -A && git commit -m "feat: track active calls per task and emit CloudWatch EMF metrics (ActiveCalls drives autoscaling)"
```

---

### Task 4: Chaos proxy in front of real model servers

**Files:**
- Create: `src/mock_provider/chaos.py`, `src/mock_provider/proxy.py`, `tests/test_proxy.py`
- Modify: `src/mock_provider/app.py` (use `chaos.py`), `src/voice_agent/cli.py` (`chaos-proxy` command)

**Interfaces:**
- Produces:
  - `mock_provider.chaos`: `ChaosState`, `FAILURE_MODES`, `add_admin_routes(app, state)`, `OUTCOMES` (now includes `"upstream_error"`);
  - `mock_provider.proxy`: `ProxyConfig.from_env()` and `create_proxy_app(config, *, client=None)`;
  - CLI `voice-agent chaos-proxy --host --port`.
- Proxy routes:
  - `POST /v1/chat/completions` → stage `llm` → `{llm_upstream}/v1/chat/completions`;
  - `POST /v1/audio/transcriptions` → `stt` → `{audio_upstream}`;
  - `POST /v1/audio/speech` → `tts` → `{audio_upstream}`;
  - `GET /health`, `GET /ready` (200 only when both upstreams answer `/health`), and the admin routes `/admin/chaos`, `/admin/stats`, `/admin/reset`.
- Env vars: `PROXY_LLM_UPSTREAM`, `PROXY_AUDIO_UPSTREAM`, `MOCK_FAILURE_RATE`, `MOCK_SEED`, `MOCK_HANG_S`, `PROXY_TIMEOUT_S` (default 60).

- [ ] **Step 1: Extract `chaos.py` from `app.py`.**
  - Move into it: `Stage`, `STAGES`, `FAILURE_MODES`, `OUTCOMES`, the `MockConfig` dataclass and `ChaosRequest`, and the `_State` class renamed `ChaosState` (with `reset`, `pick_outcome`, `latency`).
  - Add `"upstream_error"` to `OUTCOMES` after `"outage"`.
  - Add the `LATENCY` constant.
  - Add the function that registers the three admin routes, with the bodies moved verbatim from `app.py`:

```python
def add_admin_routes(app: FastAPI, state: ChaosState) -> None:
    @app.post("/admin/chaos")
    async def chaos(req: ChaosRequest) -> dict[str, Any]: ...
    @app.post("/admin/reset")
    async def reset() -> dict[str, Any]: ...
    @app.get("/admin/stats")
    async def stats() -> dict[str, Any]: ...
```

  `app.py` imports these and calls `add_admin_routes(app, state)`; its behaviour is unchanged. Run `uv run pytest tests/test_mock_provider.py -q`. Expected: PASS.

- [ ] **Step 2: Write failing proxy tests** (`tests/test_proxy.py`). The upstream is an in-process fake. The real `OpenAICompat*` adapters talk to the proxy, which proves the Round 1 swap story end to end:

```python
import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from mock_provider.chaos import MockConfig
from mock_provider.proxy import ProxyConfig, create_proxy_app
from voice_agent.providers.base import ChatMessage, ProviderError
from voice_agent.providers.openai_compat import OpenAICompatLlm, OpenAICompatStt, OpenAICompatTts


def upstream() -> FastAPI:
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def chat(request: Request) -> JSONResponse:
        body = await request.json()
        return JSONResponse({"choices": [{"message": {"content": f"echo:{body['model']}"}}]})

    @app.post("/v1/audio/transcriptions")
    async def stt(request: Request) -> JSONResponse:
        form = await request.form()
        return JSONResponse({"text": f"heard {form['model']}"})

    @app.post("/v1/audio/speech")
    async def tts() -> Response:
        return Response(b"RIFF....WAVE", media_type="audio/wav")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def proxy(rate: float) -> httpx.AsyncClient:
    up = httpx.AsyncClient(transport=httpx.ASGITransport(app=upstream()), base_url="http://up")
    config = ProxyConfig(
        chaos=MockConfig(failure_rate=rate, seed=1, hang_s=0.05),
        llm_upstream="http://up",
        audio_upstream="http://up",
        timeout_s=5,
    )
    app = create_proxy_app(config, client=up)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy")


async def test_healthy_requests_reach_the_real_servers_through_the_openai_adapters() -> None:
    async with proxy(0.0) as c:
        assert (
            await OpenAICompatLlm(c, "http://proxy/v1", model="m").complete(
                [ChatMessage("user", "hi")]
            )
            == "echo:m"
        )
        assert (
            await OpenAICompatStt(c, "http://proxy/v1", model="w").transcribe(b"RIFF") == "heard w"
        )
        assert (
            await OpenAICompatTts(c, "http://proxy/v1", model="k", voice="v").synthesize("hi")
            == b"RIFF....WAVE"
        )
        stats = (await c.get("/admin/stats")).json()["stages"]
        assert stats["llm"]["ok"] == 1 and stats["stt"]["ok"] == 1 and stats["tts"]["ok"] == 1


@pytest.mark.parametrize("stage", ["llm", "stt", "tts"])
async def test_injected_failures_surface_as_retryable_provider_errors(stage: str) -> None:
    async with proxy(1.0) as c:
        adapters = {
            "llm": lambda: OpenAICompatLlm(c, "http://proxy/v1", model="m").complete(
                [ChatMessage("user", "hi")]
            ),
            "stt": lambda: OpenAICompatStt(c, "http://proxy/v1", model="w").transcribe(b"RIFF"),
            "tts": lambda: OpenAICompatTts(c, "http://proxy/v1", model="k", voice="v").synthesize(
                "hi"
            ),
        }
        kinds = set()
        for _ in range(40):
            with pytest.raises(ProviderError) as err:
                await adapters[stage]()
            assert err.value.retryable
            kinds.add(err.value.kind)
        assert len(kinds) >= 2  # a mix of server_error / rate_limited / malformed


async def test_outage_switch_fails_every_request_for_that_stage_only() -> None:
    async with proxy(0.0) as c:
        await c.post("/admin/chaos", json={"stage": "llm", "outage_s": 30})
        r = await c.post("/v1/chat/completions", json={"model": "m", "messages": []})
        assert r.status_code == 503
        r = await c.post("/v1/audio/speech", json={"model": "k", "input": "x", "voice": "v"})
        assert r.status_code == 200


async def test_ready_reflects_upstream_health() -> None:
    async with proxy(0.0) as c:
        assert (await c.get("/ready")).status_code == 200
```

Check the `ChatMessage` constructor and the adapter signatures against `providers/base.py` and `openai_compat.py`, and adjust the test calls to match. The behaviour under test does not change.

Run `uv run pytest tests/test_proxy.py -q`. Expected: FAIL (no module `mock_provider.proxy`).

- [ ] **Step 3: Implement `proxy.py`:**

```python
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
# What a "malformed 200" looks like for each stage (each breaks the adapter's parser).
MALFORMED: dict[Stage, Response] = {
    "llm": Response('{"choices": [{"message": {"cont', media_type="application/json"),
    "stt": Response('{"result": null}', media_type="application/json"),
    "tts": Response(b"", media_type="audio/wav"),
}


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
    http = client or httpx.AsyncClient(
        timeout=httpx.Timeout(config.timeout_s, connect=5.0),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
    )
    upstreams = {"llm": config.llm_upstream.rstrip("/"), "audio": config.audio_upstream.rstrip("/")}
    app = FastAPI(title="Chaos proxy", version="0.1.0")
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
        return MALFORMED[stage]

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
```

Make sure the `stats` route returns every key in `OUTCOMES`. In `ChaosState`, stats are a `Counter`, so the keys exist on read. Then run `uv run pytest tests/test_proxy.py -q`. Expected: PASS.

The `ChaosState.pick_outcome` hang mode does not sleep in the mock app; the proxy sleeps in `injected`. Keep `app.py`'s own sleeping as it is.

- [ ] **Step 4: CLI command.** In `cli.py`, add to the docstring: `chaos-proxy  run the failure-injecting proxy in front of real model servers`. Add a subparser `chaos-proxy` with `--host` (default `0.0.0.0`) and `--port` (default `8080`), plus the handler:

```python
def _chaos_proxy(args: argparse.Namespace) -> None:
    import uvicorn

    from mock_provider.proxy import ProxyConfig, create_proxy_app

    _configure_logging("INFO")
    uvicorn.run(
        create_proxy_app(ProxyConfig.from_env()),
        host=args.host,
        port=args.port,
        log_level="warning",
    )
```

Dispatch it in `main`. Add a test to `tests/test_cli.py`: `build_parser().parse_args(["chaos-proxy"])` gives `host == "0.0.0.0"` and `port == 8080`.

- [ ] **Step 5: Lint, type-check, run the tests and commit.**
```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q
git add -A && git commit -m "feat: chaos proxy injects the Round 1 failure mix in front of vLLM and Speaches"
```

---

### Task 5: Campaign load generator, real utterances, chaos trigger over SSM

**Files:**
- Create: `src/voice_agent/campaign.py`, `tests/test_campaign.py`, `scripts/make_utterances.ps1`, `assets/utterances/*.wav` (generated), `scripts/chaos.py`
- Modify: `src/voice_agent/cli.py` (`campaign` command)

**Interfaces:**
- Consumes: `POST /v1/calls/{id}/turns`, `POST /v1/calls/{id}/end` (Task 3).
- Produces:
  - `Phase(name, minutes, calls_per_min)`;
  - `parse_profile(text) -> list[Phase]`;
  - `load_audio(directory) -> list[bytes]`;
  - `make_client(base_url, *, transport=None) -> httpx.AsyncClient`. Its cookie jar refuses all cookies; otherwise httpx would share one ALB stickiness cookie across every call and pin all traffic to a single task;
  - `Campaign(client, phases, *, audio, turns_per_call=3, gap_s=30.0, rng=None, progress_every_s=30.0, out=print)` with `async run() -> dict[str, PhaseStats]`;
  - `format_campaign(stats) -> str`;
  - CLI `voice-agent campaign --url URL --profile baseline:10:10,spike:15:50,cooldown:10:10 --turns 3 --gap 30 --audio-dir assets/utterances --seed 7 --json-out PATH`.

- [ ] **Step 1: Write failing tests** (`tests/test_campaign.py`):

```python
import json
import random

import httpx
import pytest

from voice_agent.campaign import Campaign, Phase, make_client, parse_profile


def test_parse_profile() -> None:
    assert parse_profile("baseline:10:10, spike:15:50") == [
        Phase("baseline", 10.0, 10.0),
        Phase("spike", 15.0, 50.0),
    ]
    with pytest.raises(ValueError):
        parse_profile("baseline:10")


def fake_service(seen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/end"):
            return httpx.Response(200, json={"was_active": True})
        headers = {"set-cookie": "AWSALB=task-7; Path=/"}
        return httpx.Response(
            200,
            headers=headers,
            json={
                "status": "completed",
                "action": "continue",
                "error_kind": None,
                "failed_stage": None,
            },
        )

    return httpx.MockTransport(handler)


async def test_calls_stay_sticky_and_are_ended() -> None:
    seen: list[httpx.Request] = []
    async with make_client("http://svc", transport=fake_service(seen)) as client:
        campaign = Campaign(
            client,
            [Phase("burst", 0.02, 300)],
            audio=[b"RIFF"],
            turns_per_call=2,
            gap_s=0.01,
            rng=random.Random(1),
            progress_every_s=999,
            out=lambda _: None,
        )
        stats = await campaign.run()
    turns = [r for r in seen if r.url.path.endswith("/turns")]
    ends = [r for r in seen if r.url.path.endswith("/end")]
    assert stats["burst"].calls_started >= 1
    assert len(ends) == stats["burst"].calls_started
    assert stats["burst"].turns["completed"] == len(turns) == 2 * stats["burst"].calls_started
    by_call: dict[str, list[httpx.Request]] = {}
    for r in turns:
        by_call.setdefault(r.url.path.split("/")[3], []).append(r)
    for requests in by_call.values():
        assert "cookie" not in requests[0].headers
        assert requests[1].headers["cookie"] == "AWSALB=task-7"
    assert json.loads(turns[0].content)["audio_b64"] == "UklGRg=="


async def test_http_failures_are_counted_not_raised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={})

    async with make_client("http://svc", transport=httpx.MockTransport(handler)) as client:
        campaign = Campaign(
            client,
            [Phase("p", 0.02, 300)],
            audio=[b"RIFF"],
            turns_per_call=1,
            gap_s=0.0,
            rng=random.Random(2),
            progress_every_s=999,
            out=lambda _: None,
        )
        stats = await campaign.run()
    assert stats["p"].http_failures == stats["p"].calls_started >= 1
```

Run `uv run pytest tests/test_campaign.py -q`. Expected: FAIL (no module).

- [ ] **Step 2: Implement `campaign.py`:**

```python
"""Scripted campaign traffic: a flat baseline, a campaign-window spike, a cool-down.

Calls arrive as a Poisson process at each phase's rate. A call is `turns_per_call`
turns, `gap_s` apart, each carrying a real WAV utterance, and it ends with
POST .../end so the service stops counting it. Each call keeps the ALB stickiness
cookie, so all of its turns land on one task, as a WebSocket per call would.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import random
import statistics
import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from http.cookiejar import CookieJar, DefaultCookiePolicy
from pathlib import Path
from typing import Any

import httpx


@dataclass(frozen=True)
class Phase:
    name: str
    minutes: float
    calls_per_min: float


@dataclass
class PhaseStats:
    calls_started: int = 0
    turns: Counter[str] = field(default_factory=Counter)
    http_failures: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    errors: Counter[str] = field(default_factory=Counter)

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls_started": self.calls_started,
            "turns": dict(self.turns),
            "http_failures": self.http_failures,
            "errors": dict(self.errors),
            "latency_ms": latency_summary(self.latencies_ms),
        }


def parse_profile(text: str) -> list[Phase]:
    phases = []
    for part in text.split(","):
        pieces = [p.strip() for p in part.split(":")]
        if len(pieces) != 3:
            raise ValueError(f"phase {part!r} is not name:minutes:calls_per_min")
        phases.append(Phase(pieces[0], float(pieces[1]), float(pieces[2])))
    return phases


def load_audio(directory: Path) -> list[bytes]:
    files = sorted(directory.glob("*.wav"))
    if not files:
        raise FileNotFoundError(f"no .wav files in {directory}; run scripts/make_utterances.ps1")
    return [f.read_bytes() for f in files]


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def latency_summary(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    return {
        "p50": round(statistics.median(values)),
        "p90": round(percentile(values, 0.90)),
        "p99": round(percentile(values, 0.99)),
        "max": round(max(values)),
    }


def make_client(
    base_url: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    """A client that never stores cookies. Each call tracks its own ALB stickiness
    cookie; a shared jar would pin every call to whichever task answered first."""
    no_cookies = CookieJar(policy=DefaultCookiePolicy(allowed_domains=[]))
    return httpx.AsyncClient(
        base_url=base_url,
        transport=transport,
        cookies=no_cookies,
        timeout=60,
        limits=httpx.Limits(max_connections=400, max_keepalive_connections=100),
    )


def _remember_stickiness(response: httpx.Response, jar: dict[str, str]) -> None:
    for raw in response.headers.get_list("set-cookie"):
        name, _, rest = raw.partition("=")
        if name.strip().startswith("AWSALB"):
            jar[name.strip()] = rest.split(";", 1)[0]


def _cookie(jar: dict[str, str]) -> dict[str, str]:
    return {"cookie": "; ".join(f"{k}={v}" for k, v in jar.items())} if jar else {}


class Campaign:
    def __init__(
        self,
        client: httpx.AsyncClient,
        phases: list[Phase],
        *,
        audio: list[bytes],
        turns_per_call: int = 3,
        gap_s: float = 30.0,
        rng: random.Random | None = None,
        progress_every_s: float = 30.0,
        out: Callable[[str], None] = print,
    ) -> None:
        self._client = client
        self._phases = phases
        self._audio = [base64.b64encode(a).decode("ascii") for a in audio]
        self._turns = turns_per_call
        self._gap_s = gap_s
        self._rng = rng or random.Random()
        self._progress_every_s = progress_every_s
        self._out = out
        self.stats: dict[str, PhaseStats] = {}
        self.active = 0
        self._phase = ""

    async def run(self) -> dict[str, PhaseStats]:
        started = time.monotonic()
        calls: set[asyncio.Task[None]] = set()
        progress = asyncio.create_task(self._progress(started))
        try:
            for phase in self._phases:
                self._phase = phase.name
                stats = self.stats[phase.name] = PhaseStats()
                phase_end = time.monotonic() + phase.minutes * 60
                rate_per_s = phase.calls_per_min / 60
                while True:
                    gap = self._rng.expovariate(rate_per_s) if rate_per_s > 0 else float("inf")
                    if time.monotonic() + gap >= phase_end:
                        await asyncio.sleep(max(0.0, phase_end - time.monotonic()))
                        break
                    await asyncio.sleep(gap)
                    task = asyncio.create_task(self._call(stats))
                    calls.add(task)
                    task.add_done_callback(calls.discard)
            self._phase = "draining"
            if calls:
                await asyncio.gather(*calls)
        finally:
            progress.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await progress
        return self.stats

    async def _call(self, stats: PhaseStats) -> None:
        call_id = f"call-{uuid.uuid4().hex[:10]}"
        jar: dict[str, str] = {}
        stats.calls_started += 1
        self.active += 1
        try:
            for n in range(self._turns):
                if n:
                    await asyncio.sleep(self._gap_s)
                body = {"turn_id": f"t{n}", "audio_b64": self._rng.choice(self._audio)}
                sent = time.perf_counter()
                try:
                    r = await self._client.post(
                        f"/v1/calls/{call_id}/turns", json=body, headers=_cookie(jar)
                    )
                except httpx.HTTPError as exc:
                    stats.http_failures += 1
                    stats.errors[type(exc).__name__] += 1
                    continue
                stats.latencies_ms.append((time.perf_counter() - sent) * 1000)
                _remember_stickiness(r, jar)
                if r.status_code != 200:
                    stats.http_failures += 1
                    stats.errors[f"HTTP {r.status_code}"] += 1
                    continue
                data = r.json()
                stats.turns[data["status"]] += 1
                if data.get("error_kind"):
                    stats.errors[f"{data['failed_stage']}:{data['error_kind']}"] += 1
            with contextlib.suppress(httpx.HTTPError):
                await self._client.post(f"/v1/calls/{call_id}/end", headers=_cookie(jar))
        finally:
            self.active -= 1

    async def _progress(self, started: float) -> None:
        while True:
            await asyncio.sleep(self._progress_every_s)
            stats = self.stats.get(self._phase) or PhaseStats()
            recent = stats.latencies_ms[-200:]
            done = sum(stats.turns.values())
            self._out(
                f"t+{(time.monotonic() - started) / 60:5.1f}m  phase={self._phase:<9} "
                f"active_calls={self.active:<4} turns={done:<5} "
                f"degraded={done - stats.turns['completed']:<4} http_fail={stats.http_failures:<3} "
                f"p50={percentile(recent, 0.5):.0f}ms p99={percentile(recent, 0.99):.0f}ms"
            )


def format_campaign(stats: dict[str, PhaseStats]) -> str:
    lines = [
        f"{'phase':<10} {'calls':>6} {'turns':>6} {'completed':>9} {'degraded':>8} "
        f"{'http_fail':>9} {'p50':>6} {'p90':>6} {'p99':>6}"
    ]
    for name, s in stats.items():
        total = sum(s.turns.values())
        lat = latency_summary(s.latencies_ms)
        lines.append(
            f"{name:<10} {s.calls_started:>6} {total:>6} {s.turns['completed']:>9} "
            f"{total - s.turns['completed']:>8} {s.http_failures:>9} "
            f"{lat.get('p50', 0):>6.0f} {lat.get('p90', 0):>6.0f} {lat.get('p99', 0):>6.0f}"
        )
    return "\n".join(lines)
```

Run `uv run pytest tests/test_campaign.py -q`. Expected: PASS, in about 2 s.

- [ ] **Step 3: CLI.** Add the subparser `campaign` with these arguments:
  - `--url` (required);
  - `--profile` (default `baseline:10:10,spike:15:50,cooldown:10:10`);
  - `--turns` (int, 3);
  - `--gap` (float, 30);
  - `--audio-dir` (default `assets/utterances`);
  - `--seed` (int, optional);
  - `--json-out` (optional path).

  The handler:

```python
async def _campaign(args: argparse.Namespace) -> int:
    from pathlib import Path

    import httpx

    from voice_agent.campaign import (
        Campaign,
        format_campaign,
        load_audio,
        make_client,
        parse_profile,
    )

    phases = parse_profile(args.profile)
    async with make_client(args.url) as client:
        campaign = Campaign(
            client,
            phases,
            audio=load_audio(Path(args.audio_dir)),
            turns_per_call=args.turns,
            gap_s=args.gap,
            rng=random.Random(args.seed),
        )
        stats = await campaign.run()
    print(format_campaign(stats))
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(
            json.dumps({name: s.as_dict() for name, s in stats.items()}, indent=2)
        )
    return 0 if all(s.http_failures == 0 for s in stats.values()) else 1
```

`import random` goes at the top of `cli.py`. In `main`: `if args.command == "campaign": return asyncio.run(_campaign(args))`.

- [ ] **Step 4: Utterance generator** (`scripts/make_utterances.ps1`):

```powershell
# Synthesize the demo caller utterances as 16 kHz mono 16-bit WAV with Windows' built-in
# speech engine (no network, no model). Run once:  powershell -File scripts/make_utterances.ps1
Add-Type -AssemblyName System.Speech
$out = Join-Path $PSScriptRoot "..\assets\utterances"
New-Item -ItemType Directory -Force -Path $out | Out-Null
$lines = @(
  "I can pay half now and the rest next Friday.",
  "Who is this calling?",
  "I already paid this last week.",
  "Can I speak to a real person?",
  "I lost my job, I need more time.",
  "Okay, what is the balance?"
)
$format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,
  [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$i = 1
foreach ($line in $lines) {
  $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
  $path = Join-Path $out ("u{0}.wav" -f $i)
  $synth.SetOutputToWaveFile($path, $format)
  $synth.Speak($line)
  $synth.Dispose()
  Write-Output ("wrote {0}" -f $path)
  $i++
}
```

Run `powershell -NoProfile -File scripts/make_utterances.ps1`. Expected: six files `assets/utterances/u1.wav`..`u6.wav`, each 50–150 KB. Check with `python -c "import wave; w=wave.open('assets/utterances/u1.wav'); print(w.getframerate(), w.getnchannels())"`, which should print `16000 1`.

- [ ] **Step 5: Chaos trigger over SSM** (`scripts/chaos.py`):

```python
"""Drive the GPU host's chaos proxy from your laptop, without exposing it.

The proxy listens only inside the VPC. This sends `curl` to it over SSM Run Command,
authenticated by your AWS profile.

  uv run python scripts/chaos.py stats
  uv run python scripts/chaos.py outage --stage llm --seconds 60
  uv run python scripts/chaos.py rate --rate 0.2
  uv run python scripts/chaos.py reset
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import time

import boto3

TAG_NAME = "collectionsinference-gpu"


def instance_id(ec2: object) -> str:
    reservations = ec2.describe_instances(  # type: ignore[attr-defined]
        Filters=[
            {"Name": "tag:Name", "Values": [TAG_NAME]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ]
    )["Reservations"]
    ids = [i["InstanceId"] for r in reservations for i in r["Instances"]]
    if not ids:
        sys.exit('no running GPU host (is model_tier = "gpu" applied?)')
    return str(ids[0])


def curl(method: str, path: str, body: dict[str, object] | None) -> str:
    command = f"curl -sS -X {method} http://localhost:8080{path}"
    if body is not None:
        command += f" -H 'content-type: application/json' -d {shlex.quote(json.dumps(body))}"
    return command


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--profile", default="predixion")
    parser.add_argument("--region", default="ap-south-1")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("stats")
    sub.add_parser("reset")
    outage = sub.add_parser("outage")
    outage.add_argument("--stage", choices=["stt", "llm", "tts"], required=True)
    outage.add_argument("--seconds", type=float, required=True)
    rate = sub.add_parser("rate")
    rate.add_argument("--rate", type=float, required=True)
    rate.add_argument("--stage", choices=["stt", "llm", "tts"])
    args = parser.parse_args()

    if args.action == "stats":
        command = curl("GET", "/admin/stats", None)
    elif args.action == "reset":
        command = curl("POST", "/admin/reset", None)
    elif args.action == "outage":
        command = curl("POST", "/admin/chaos", {"stage": args.stage, "outage_s": args.seconds})
    else:
        command = curl("POST", "/admin/chaos", {"stage": args.stage, "failure_rate": args.rate})

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    target = instance_id(session.client("ec2"))
    ssm = session.client("ssm")
    sent = ssm.send_command(
        InstanceIds=[target], DocumentName="AWS-RunShellScript", Parameters={"commands": [command]}
    )
    command_id = sent["Command"]["CommandId"]
    for _ in range(30):
        time.sleep(1)
        try:
            result = ssm.get_command_invocation(CommandId=command_id, InstanceId=target)
        except ssm.exceptions.InvocationDoesNotExist:
            continue
        if result["Status"] in ("Success", "Failed", "Cancelled", "TimedOut"):
            print(result["StandardOutputContent"] or result["StandardErrorContent"])
            return 0 if result["Status"] == "Success" else 1
    print("timed out waiting for SSM")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

Check: `uv run python scripts/chaos.py --help` prints the usage. Before Task 13 nothing exists to call yet.

- [ ] **Step 6: Lint, type-check, run the tests and commit.** Add `scripts` to ruff's coverage; it is already covered by `ruff check .`. Then:
```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q
git add -A && git commit -m "feat: campaign load generator (Poisson arrivals, sticky calls, real WAV utterances) and SSM chaos trigger"
```

---

### Task 6: Ops scripts: IAM wildcard check and teardown proof

**Files:**
- Create: `scripts/__init__.py` (empty), `scripts/check_iam.py`, `scripts/teardown_check.py`, `tests/scripts/__init__.py`, `tests/scripts/test_check_iam.py`, `tests/scripts/test_teardown_check.py`
- Modify: `pyproject.toml` (add `moto[ec2,elbv2,ecs,ecr,logs,autoscaling,cloudwatch,sns]` to the dev extras)

**Interfaces:**
- Produces:
  - `check_iam.findings(doc: dict) -> list[Finding]` and `check_iam.main(argv) -> int`;
  - `teardown_check.run_checks(session) -> dict[str, list[str]]` and `teardown_check.main(argv) -> int`, which writes `teardown/teardown-check-<UTC>.log`.

- [ ] **Step 1: Write failing tests for `check_iam`** (`tests/scripts/test_check_iam.py`):

```python
import json

from scripts.check_iam import findings


def plan(config_resources: list[dict], value_resources: list[dict] | None = None) -> dict:
    return {
        "configuration": {"root_module": {"resources": config_resources}},
        "planned_values": {"root_module": {"resources": value_resources or []}},
    }


def doc_resource(name: str, statements: list[dict]) -> dict:
    return {
        "address": f"data.aws_iam_policy_document.{name}",
        "mode": "data",
        "type": "aws_iam_policy_document",
        "expressions": {
            "statement": [{k: {"constant_value": v} for k, v in s.items()} for s in statements]
        },
    }


def test_explicit_actions_pass() -> None:
    doc = plan(
        [
            doc_resource(
                "task", [{"effect": "Allow", "actions": ["dynamodb:GetItem"], "resources": ["*"]}]
            )
        ]
    )
    assert findings(doc) == []


def test_wildcard_action_in_a_policy_document_fails() -> None:
    doc = plan([doc_resource("bad", [{"actions": ["dynamodb:*"]}])])
    [f] = findings(doc)
    assert f.address == "data.aws_iam_policy_document.bad" and "dynamodb:*" in f.problem


def test_not_actions_fails() -> None:
    doc = plan([doc_resource("bad", [{"not_actions": ["iam:PassRole"]}])])
    assert "NotAction" in findings(doc)[0].problem


def test_known_inline_policy_json_is_checked_too() -> None:
    policy = json.dumps({"Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]})
    doc = plan(
        [],
        [
            {
                "address": "aws_iam_role_policy.x",
                "type": "aws_iam_role_policy",
                "values": {"policy": policy},
            }
        ],
    )
    assert findings(doc)[0].address == "aws_iam_role_policy.x"


def test_aws_managed_policy_attachments_fail() -> None:
    doc = plan(
        [],
        [
            {
                "address": "aws_iam_role_policy_attachment.a",
                "type": "aws_iam_role_policy_attachment",
                "values": {"policy_arn": "arn:aws:iam::aws:policy/AmazonS3FullAccess"},
            }
        ],
    )
    assert "AWS-managed" in findings(doc)[0].problem


def test_child_modules_are_scanned() -> None:
    doc = {
        "configuration": {
            "root_module": {
                "module_calls": {
                    "m": {
                        "module": {"resources": [doc_resource("bad", [{"actions": ["s3:Get*"]}])]}
                    }
                }
            }
        }
    }
    assert len(findings(doc)) == 1
```

Run: FAIL.

- [ ] **Step 2: Implement `scripts/check_iam.py`:**

```python
"""Fail if any IAM policy Terraform manages allows a wildcard action.

Checks, from `terraform show -json` output (a saved plan or the state):
* every `data.aws_iam_policy_document` statement in the configuration (known before
  apply, so this runs in CI on a plan);
* every known policy JSON in planned/state values (inline role policies, managed
  policies, resource policies);
* that no AWS-managed policy is attached (we cannot vouch for its actions).

  terraform -chdir=infra plan -out=tfplan
  terraform -chdir=infra show -json tfplan > plan.json
  uv run python scripts/check_iam.py plan.json
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

POLICY_ATTRIBUTES = {
    "aws_iam_policy": "policy",
    "aws_iam_role_policy": "policy",
    "aws_iam_user_policy": "policy",
    "aws_iam_group_policy": "policy",
    "aws_cloudwatch_log_resource_policy": "policy_document",
    "aws_ecr_repository_policy": "policy",
    "aws_sns_topic_policy": "policy",
}
ATTACHMENTS = {
    "aws_iam_role_policy_attachment",
    "aws_iam_user_policy_attachment",
    "aws_iam_group_policy_attachment",
    "aws_iam_policy_attachment",
}


@dataclass(frozen=True)
class Finding:
    address: str
    problem: str


def _modules(module: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield module
    for call in (module.get("module_calls") or {}).values():
        yield from _modules(call.get("module") or {})
    for child in module.get("child_modules") or []:
        yield from _modules(child)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _bad_actions(actions: list[Any]) -> list[str]:
    return [a for a in actions if isinstance(a, str) and "*" in a]


def _config_findings(doc: dict[str, Any]) -> Iterator[Finding]:
    root = (doc.get("configuration") or {}).get("root_module") or {}
    for module in _modules(root):
        for res in module.get("resources") or []:
            if res.get("type") != "aws_iam_policy_document":
                continue
            for statement in _as_list((res.get("expressions") or {}).get("statement")):
                if "not_actions" in statement:
                    yield Finding(res["address"], "uses NotAction")
                actions = _as_list((statement.get("actions") or {}).get("constant_value"))
                if bad := _bad_actions(actions):
                    yield Finding(res["address"], f"wildcard action(s): {', '.join(bad)}")


def _value_findings(doc: dict[str, Any]) -> Iterator[Finding]:
    values = doc.get("planned_values") or doc.get("values") or {}
    for module in _modules(values.get("root_module") or {}):
        for res in module.get("resources") or []:
            kind, attrs = res.get("type"), res.get("values") or {}
            if kind in ATTACHMENTS and str(attrs.get("policy_arn", "")).startswith(
                "arn:aws:iam::aws:policy/"
            ):
                yield Finding(res["address"], f"attaches AWS-managed {attrs['policy_arn']}")
            text = (
                attrs.get(POLICY_ATTRIBUTES.get(kind, ""), None)
                if kind in POLICY_ATTRIBUTES
                else None
            )
            if not text:
                continue
            for statement in _as_list(json.loads(text).get("Statement")):
                if "NotAction" in statement:
                    yield Finding(res["address"], "uses NotAction")
                if bad := _bad_actions(_as_list(statement.get("Action"))):
                    yield Finding(res["address"], f"wildcard action(s): {', '.join(bad)}")


def findings(doc: dict[str, Any]) -> list[Finding]:
    return list(_config_findings(doc)) + list(_value_findings(doc))


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print(__doc__)
        return 2
    problems = findings(json.loads(Path(args[0]).read_text(encoding="utf-8")))
    for f in problems:
        print(f"FAIL  {f.address}: {f.problem}")
    print(
        "IAM check: " + ("no wildcard actions" if not problems else f"{len(problems)} problem(s)")
    )
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Run `uv run pytest tests/scripts/test_check_iam.py -q`. Expected: PASS.

- [ ] **Step 3: Write failing tests for `teardown_check`** (`tests/scripts/test_teardown_check.py`):

```python
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from scripts.teardown_check import main, run_checks


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch):
    for var in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        yield boto3.Session(region_name="ap-south-1")


def test_an_empty_account_is_clean(aws) -> None:
    assert all(not found for found in run_checks(aws).values())


def test_leftovers_are_reported(aws) -> None:
    aws.client("ec2").create_volume(AvailabilityZone="ap-south-1a", Size=10)
    aws.client("dynamodb").create_table(
        TableName="collectionsinference-state",
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
    )
    results = run_checks(aws)
    assert len(results["EBS volumes"]) == 1
    assert results["DynamoDB tables"] == ["collectionsinference-state"]


def test_main_writes_a_log_and_exits_nonzero_when_not_clean(
    aws, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("scripts.teardown_check.make_session", lambda profile, region: aws)
    aws.client("ec2").create_volume(AvailabilityZone="ap-south-1a", Size=10)
    assert main(["--out-dir", str(tmp_path)]) == 1
    [log] = list(tmp_path.glob("teardown-check-*.log"))
    assert "NOT CLEAN" in log.read_text()
```

Run: FAIL.

- [ ] **Step 4: Implement `scripts/teardown_check.py`:**

```python
"""Prove the stack is gone: list anything left in the region that costs money.

  uv run python scripts/teardown_check.py            # profile predixion, ap-south-1
Writes teardown/teardown-check-<UTC>.log and exits 0 only when nothing is left.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3


def make_session(profile: str, region: str) -> Any:
    return boto3.Session(profile_name=profile, region_name=region)


def _pages(client: Any, operation: str, key: str, **kwargs: Any) -> list[Any]:
    paginator = client.get_paginator(operation)
    return [item for page in paginator.paginate(**kwargs) for item in page.get(key, [])]


def _instances(s: Any) -> list[str]:
    return [
        f"{i['InstanceId']} {i['InstanceType']} {i['State']['Name']}"
        for r in _pages(s.client("ec2"), "describe_instances", "Reservations")
        for i in r["Instances"]
        if i["State"]["Name"] != "terminated"
    ]


CHECKS: dict[str, Callable[[Any], list[str]]] = {
    "EC2 instances": _instances,
    "NAT gateways": lambda s: [
        f"{n['NatGatewayId']} {n['State']}"
        for n in _pages(s.client("ec2"), "describe_nat_gateways", "NatGateways")
        if n["State"] not in ("deleted",)
    ],
    "Load balancers": lambda s: [
        lb["LoadBalancerName"]
        for lb in _pages(s.client("elbv2"), "describe_load_balancers", "LoadBalancers")
    ],
    "Elastic IPs": lambda s: [
        a.get("PublicIp", "?") for a in s.client("ec2").describe_addresses()["Addresses"]
    ],
    "EBS volumes": lambda s: [
        f"{v['VolumeId']} {v['Size']}GB {v['State']}"
        for v in _pages(s.client("ec2"), "describe_volumes", "Volumes")
    ],
    "EBS snapshots": lambda s: [
        sn["SnapshotId"]
        for sn in _pages(s.client("ec2"), "describe_snapshots", "Snapshots", OwnerIds=["self"])
    ],
    "Network interfaces": lambda s: [
        f"{n['NetworkInterfaceId']} {n.get('Description', '')}"
        for n in _pages(s.client("ec2"), "describe_network_interfaces", "NetworkInterfaces")
    ],
    "VPC endpoints": lambda s: [
        e["VpcEndpointId"]
        for e in _pages(s.client("ec2"), "describe_vpc_endpoints", "VpcEndpoints")
        if e["State"].lower() not in ("deleted",)
    ],
    "Non-default VPCs": lambda s: [
        v["VpcId"]
        for v in _pages(s.client("ec2"), "describe_vpcs", "Vpcs")
        if not v.get("IsDefault")
    ],
    "Auto Scaling groups": lambda s: [
        g["AutoScalingGroupName"]
        for g in _pages(
            s.client("autoscaling"), "describe_auto_scaling_groups", "AutoScalingGroups"
        )
    ],
    "ECS clusters": lambda s: [
        c["clusterName"]
        for arns in [s.client("ecs").list_clusters()["clusterArns"]]
        if arns
        for c in s.client("ecs").describe_clusters(clusters=arns)["clusters"]
        if c["status"] == "ACTIVE"
    ],
    "DynamoDB tables": lambda s: _pages(s.client("dynamodb"), "list_tables", "TableNames"),
    "ECR repositories": lambda s: [
        r["repositoryName"]
        for r in _pages(s.client("ecr"), "describe_repositories", "repositories")
    ],
    "CloudWatch log groups": lambda s: [
        g["logGroupName"] for g in _pages(s.client("logs"), "describe_log_groups", "logGroups")
    ],
    "CloudWatch alarms": lambda s: [
        a["AlarmName"] for a in _pages(s.client("cloudwatch"), "describe_alarms", "MetricAlarms")
    ],
    "CloudWatch dashboards": lambda s: [
        d["DashboardName"]
        for d in _pages(s.client("cloudwatch"), "list_dashboards", "DashboardEntries")
    ],
    "SNS topics": lambda s: [
        t["TopicArn"] for t in _pages(s.client("sns"), "list_topics", "Topics")
    ],
}


def run_checks(session: Any) -> dict[str, list[str]]:
    return {name: check(session) for name, check in CHECKS.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="predixion")
    parser.add_argument("--region", default="ap-south-1")
    parser.add_argument("--out-dir", default="teardown")
    args = parser.parse_args(argv)
    session = make_session(args.profile, args.region)
    identity = session.client("sts").get_caller_identity()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    results = run_checks(session)
    leftovers = sum(len(v) for v in results.values())
    lines = [
        f"Teardown check {stamp}",
        f"account {identity['Account']}  identity {identity['Arn']}  region {args.region}",
        "",
    ]
    for name, found in results.items():
        lines.append(f"[{'LEFT' if found else ' ok '}] {name}: {len(found)}")
        lines += [f"         - {item}" for item in found]
    lines += [
        "",
        "CLEAN: no billable resources remain"
        if not leftovers
        else f"NOT CLEAN: {leftovers} resource(s) remain",
    ]
    text = "\n".join(lines)
    print(text)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"teardown-check-{stamp}.log").write_text(text + "\n", encoding="utf-8")
    return 0 if not leftovers else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

Run `uv run pytest tests/scripts -q`. Expected: PASS. If moto lacks a paginator for an operation, replace that check's `_pages` call with the direct client call and its single page.

- [ ] **Step 5: Lint, type-check, run the tests and commit.**
```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q
git add -A && git commit -m "feat: IAM wildcard checker and teardown proof script"
```

---

### Task 7: Terraform foundation: providers, network, ECR + image, DynamoDB, IAM, logs

**Files:**
- Create:
  - `infra/versions.tf`, `infra/providers.tf`, `infra/variables.tf`, `infra/locals.tf`;
  - `infra/network.tf`, `infra/ecr.tf`, `infra/dynamodb.tf`, `infra/iam.tf`, `infra/logs.tf`;
  - `infra/outputs.tf`, `infra/terraform.tfvars.example`;
  - `.dockerignore` (create or extend).
- Modify: `.gitignore`

**Interfaces:**
- Consumes:
  - `TABLE_SCHEMA` (Task 2): table `collectionsinference-state`, keys `pk`/`sk`, GSI `by_status` on `gsi1pk`/`gsi1sk`;
  - the image's CLI commands `serve`, `mock` and `chaos-proxy` (Tasks 3–4).
- Produces, for Tasks 8–10:
  - `aws_vpc.main`, `aws_subnet.public[*]`, `aws_subnet.private[*]`;
  - `aws_ecr_repository.app`, `local.app_image` (the repo URL `@sha256` digest);
  - `aws_dynamodb_table.state`;
  - `aws_iam_role.execution`, `aws_iam_role.task`, `aws_iam_instance_profile.gpu`;
  - `aws_cloudwatch_log_group.app` (`/ecs/collectionsinference`) and `aws_cloudwatch_log_group.gpu` (`/gpu/collectionsinference`);
  - `local.name = "collectionsinference"`, `local.metric_namespace = "CollectionsChallenge"`, `local.account_id`.

- [ ] **Step 1: `.gitignore` and `.dockerignore`.** Append to `.gitignore`:

```
# Terraform
infra/.terraform/
infra/*.tfstate
infra/*.tfstate.*
infra/terraform.tfvars
infra/tfplan
infra/plan.json
# Load-test output
results/
```

`.dockerignore` (whole file):

```
.git
.venv
infra
docs
assets
teardown
results
tests
data
**/__pycache__
*.db*
```

- [ ] **Step 2: `infra/versions.tf` and `infra/providers.tf`:**

```hcl
terraform {
  required_version = ">= 1.9"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.6"
    }
    docker = {
      source  = "kreuzwerker/docker"
      version = ">= 3.0, < 5.0"
    }
  }
}
```

```hcl
provider "aws" {
  region  = var.region
  profile = var.aws_profile
  default_tags {
    tags = {
      Project   = local.name
      ManagedBy = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_ecr_authorization_token" "this" {}

# Builds the app image locally and pushes it to ECR, so one `terraform apply` deploys code too.
provider "docker" {
  host = var.docker_host
  registry_auth {
    address  = replace(data.aws_ecr_authorization_token.this.proxy_endpoint, "https://", "")
    username = data.aws_ecr_authorization_token.this.user_name
    password = data.aws_ecr_authorization_token.this.password
  }
}
```

- [ ] **Step 3: `infra/variables.tf`:**

```hcl
variable "region" {
  type    = string
  default = "ap-south-1"
}

variable "aws_profile" {
  description = "AWS CLI profile for this project only."
  type        = string
  default     = "predixion"
}

variable "docker_host" {
  description = "Docker daemon. Windows: npipe:////./pipe/docker_engine; Linux/macOS: unix:///var/run/docker.sock"
  type        = string
  default     = "npipe:////./pipe/docker_engine"
}

variable "az_ids" {
  description = "AZ IDs (not names: names differ per account). g5 is offered only in aps1-az1 and aps1-az3."
  type        = list(string)
  default     = ["aps1-az1", "aps1-az3"]
}

variable "allowed_cidrs" {
  description = "CIDRs allowed to reach the ALB (your IP as x.x.x.x/32)."
  type        = list(string)
}

variable "model_tier" {
  description = "mock: mock provider on Fargate (cheap infra check). gpu: g5.xlarge with vLLM + Speaches."
  type        = string
  default     = "mock"
  validation {
    condition     = contains(["mock", "gpu"], var.model_tier)
    error_message = "model_tier must be \"mock\" or \"gpu\"."
  }
}

variable "task_cpu" {
  type    = number
  default = 1024
}

variable "task_memory" {
  type    = number
  default = 2048
}

variable "min_tasks" {
  type    = number
  default = 2
}

variable "max_tasks" {
  type    = number
  default = 10
}

variable "target_active_calls" {
  description = "Target tracking: average active calls per task."
  type        = number
  default     = 10
}

variable "campaign_prewarm" {
  description = "Optional scheduled pre-warm: start/end are at(...) or cron(...) expressions in UTC."
  type = object({
    start     = string
    end       = string
    min_tasks = number
  })
  default = null
}

variable "failure_rate" {
  description = "Injected provider failure rate (Round 1: 20%)."
  type        = number
  default     = 0.2
}

variable "turn_deadline_s" {
  type    = number
  default = 20
}

variable "stage_timeouts_s" {
  description = "Per-attempt timeouts; set from measured p99 on the GPU (Task 13)."
  type        = object({ stt = number, llm = number, tts = number })
  default     = { stt = 5, llm = 8, tts = 5 }
}

variable "llm_hf_model" {
  type    = string
  default = "Qwen/Qwen2.5-7B-Instruct-AWQ"
}

variable "llm_model" {
  description = "Name vLLM serves the model under."
  type        = string
  default     = "qwen2.5-7b-instruct"
}

variable "stt_model" {
  type    = string
  default = "Systran/faster-whisper-small"
}

variable "tts_model" {
  type    = string
  default = "speaches-ai/Kokoro-82M-v1.0-ONNX"
}

variable "tts_voice" {
  type    = string
  default = "af_heart"
}

variable "vllm_image" {
  type    = string
  default = "vllm/vllm-openai:v0.31.0"
}

variable "speaches_image" {
  type    = string
  default = "ghcr.io/speaches-ai/speaches:latest-cuda-12.6.3"
}

variable "gpu_instance_type" {
  type    = string
  default = "g5.xlarge"
}

variable "gpu_max_hours" {
  description = "Dead-man switch: the GPU ASG scales to zero this many hours after it is created."
  type        = number
  default     = 4
}

variable "compose_version" {
  type    = string
  default = "v2.39.4"
}

variable "alert_email" {
  description = "Optional email for alarm notifications (confirm the SNS subscription email)."
  type        = string
  default     = null
}

variable "baseline_p99_ms" {
  description = "Measured p99 turn latency at baseline; the runbook trigger is 3x this."
  type        = number
  default     = 3000
}

variable "log_retention_days" {
  type    = number
  default = 7
}
```

- [ ] **Step 4: `infra/locals.tf`:**

```hcl
locals {
  name             = "collectionsinference"
  metric_namespace = "CollectionsChallenge"
  service_name     = "voice-agent"
  account_id       = data.aws_caller_identity.current.account_id
  status_index     = "by_status"
  provider_url     = "http://${aws_lb.models.dns_name}:8080/v1"
}
```

- [ ] **Step 5: `infra/network.tf`:**

```hcl
data "aws_availability_zones" "selected" {
  state = "available"
  filter {
    name   = "zone-id"
    values = var.az_ids
  }
}

locals {
  azs = data.aws_availability_zones.selected.names
}

resource "aws_vpc" "main" {
  cidr_block           = "10.40.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = local.name }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = local.name }
}

resource "aws_subnet" "public" {
  count             = length(local.azs)
  vpc_id            = aws_vpc.main.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index)
  tags              = { Name = "${local.name}-public-${local.azs[count.index]}", Tier = "public" }
}

resource "aws_subnet" "private" {
  count             = length(local.azs)
  vpc_id            = aws_vpc.main.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(aws_vpc.main.cidr_block, 7, 5 + count.index) # /23 each
  tags              = { Name = "${local.name}-private-${local.azs[count.index]}", Tier = "private" }
}

# One NAT gateway (cost): private egress for image and model downloads. Production would
# run one per AZ so an AZ outage cannot cut the other AZ's egress.
resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = { Name = "${local.name}-nat" }
}

resource "aws_nat_gateway" "main" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id
  tags          = { Name = local.name }
  depends_on    = [aws_internet_gateway.main]
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table_association" "public" {
  count          = length(aws_subnet.public)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main.id
  }
  tags = { Name = "${local.name}-private" }
}

resource "aws_route_table_association" "private" {
  count          = length(aws_subnet.private)
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# Gateway endpoints are free: DynamoDB traffic and ECR image layers (served from S3)
# bypass the NAT gateway and its per-GB charge.
resource "aws_vpc_endpoint" "dynamodb" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]
  tags              = { Name = "${local.name}-dynamodb" }
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]
  tags              = { Name = "${local.name}-s3" }
}
```

- [ ] **Step 6: `infra/ecr.tf`:**

```hcl
resource "aws_ecr_repository" "app" {
  name                 = local.name
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true # `terraform destroy` must leave nothing behind
  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the last 5 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 5 }
      action       = { type = "expire" }
    }]
  })
}

locals {
  app_root  = abspath("${path.module}/..")
  src_files = sort(concat(tolist(fileset(local.app_root, "src/**/*.py")), ["pyproject.toml", "uv.lock", "Dockerfile", "README.md"]))
  src_hash  = substr(sha1(join("", [for f in local.src_files : filesha1("${local.app_root}/${f}")])), 0, 12)
}

resource "docker_image" "app" {
  name = "${aws_ecr_repository.app.repository_url}:${local.src_hash}"
  build {
    context  = local.app_root
    platform = "linux/amd64"
  }
  triggers     = { src = local.src_hash }
  keep_locally = true
}

resource "docker_registry_image" "app" {
  name          = docker_image.app.name
  keep_remotely = true
  triggers      = { src = local.src_hash }
}

locals {
  # Pinned by digest: what was tested is exactly what runs.
  app_image = "${aws_ecr_repository.app.repository_url}@${docker_registry_image.app.sha256_digest}"
}
```

- [ ] **Step 7: `infra/dynamodb.tf`.** This must match `TABLE_SCHEMA` in `src/voice_agent/store/dynamodb.py`:

```hcl
resource "aws_dynamodb_table" "state" {
  name         = "${local.name}-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }
  attribute {
    name = "gsi1pk"
    type = "S"
  }
  attribute {
    name = "gsi1sk"
    type = "S"
  }

  global_secondary_index {
    name            = local.status_index
    hash_key        = "gsi1pk"
    range_key       = "gsi1sk"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }
  server_side_encryption {
    enabled = true
  }
}
```

- [ ] **Step 8: `infra/logs.tf`:**

```hcl
resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${local.name}"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "gpu" {
  name              = "/gpu/${local.name}"
  retention_in_days = var.log_retention_days
}
```

- [ ] **Step 9: `infra/iam.tf`.** Explicit actions only:

```hcl
# ---- ECS task execution role: pull the image, write logs ------------------------------
data "aws_iam_policy_document" "ecs_tasks_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

data "aws_iam_policy_document" "execution" {
  statement {
    sid       = "EcrAuthToken"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # this action has no resource-level scoping
  }
  statement {
    sid       = "EcrPull"
    actions   = ["ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"]
    resources = [aws_ecr_repository.app.arn]
  }
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.app.arn}:*"]
  }
}

resource "aws_iam_role" "execution" {
  name               = "${local.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role_policy" "execution" {
  name   = "pull-and-log"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution.json
}

# ---- ECS task role: the app itself. DynamoDB only; EMF needs no PutMetricData. -------
data "aws_iam_policy_document" "task" {
  statement {
    sid = "TurnsAndDeadLetters"
    # TransactWriteItems is authorised through these per-item actions.
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:ConditionCheckItem",
      "dynamodb:Query",
    ]
    resources = [
      aws_dynamodb_table.state.arn,
      "${aws_dynamodb_table.state.arn}/index/${local.status_index}",
    ]
  }
}

resource "aws_iam_role" "task" {
  name               = "${local.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role_policy" "task" {
  name   = "state-table"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}

# ---- GPU host: pull the proxy image, ship container logs, accept SSM Run Command -----
data "aws_iam_policy_document" "ec2_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "gpu" {
  statement {
    sid       = "EcrAuthToken"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid       = "EcrPull"
    actions   = ["ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"]
    resources = [aws_ecr_repository.app.arn]
  }
  statement {
    sid       = "ContainerLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.gpu.arn}:*"]
  }
  statement {
    sid = "SsmAgent" # the explicit action list of AmazonSSMManagedInstanceCore
    actions = [
      "ssm:DescribeAssociation",
      "ssm:GetDeployablePatchSnapshotForInstance",
      "ssm:GetDocument",
      "ssm:DescribeDocument",
      "ssm:GetManifest",
      "ssm:ListAssociations",
      "ssm:ListInstanceAssociations",
      "ssm:PutInventory",
      "ssm:PutComplianceItems",
      "ssm:PutConfigurePackageResult",
      "ssm:UpdateAssociationStatus",
      "ssm:UpdateInstanceAssociationStatus",
      "ssm:UpdateInstanceInformation",
      "ssmmessages:CreateControlChannel",
      "ssmmessages:CreateDataChannel",
      "ssmmessages:OpenControlChannel",
      "ssmmessages:OpenDataChannel",
      "ec2messages:AcknowledgeMessage",
      "ec2messages:DeleteMessage",
      "ec2messages:FailMessage",
      "ec2messages:GetEndpoint",
      "ec2messages:GetMessages",
      "ec2messages:SendReply",
    ]
    resources = ["*"] # the SSM agent APIs are not resource-scoped
  }
}

resource "aws_iam_role" "gpu" {
  name               = "${local.name}-gpu"
  assume_role_policy = data.aws_iam_policy_document.ec2_trust.json
}

resource "aws_iam_role_policy" "gpu" {
  name   = "proxy-logs-ssm"
  role   = aws_iam_role.gpu.id
  policy = data.aws_iam_policy_document.gpu.json
}

resource "aws_iam_instance_profile" "gpu" {
  name = "${local.name}-gpu"
  role = aws_iam_role.gpu.name
}
```

- [ ] **Step 10: `infra/outputs.tf`** (Tasks 8–10 append to it) and **`infra/terraform.tfvars.example`:**

```hcl
output "state_table" {
  value = aws_dynamodb_table.state.name
}

output "app_image" {
  value = local.app_image
}
```

```hcl
# Copy to terraform.tfvars (git-ignored) and edit.
aws_profile   = "predixion"
allowed_cidrs = ["203.0.113.7/32"] # your IP: (Invoke-RestMethod https://checkip.amazonaws.com).Trim() + "/32"
model_tier    = "mock"             # "gpu" once the G-instance quota is approved
# alert_email = "you@example.com"
# campaign_prewarm = { start = "at(2026-10-08T10:00:00)", end = "at(2026-10-08T10:40:00)", min_tasks = 6 }
```

- [ ] **Step 11: Validate locally.** Tasks 8–10 are not written yet, so `local.provider_url` references `aws_lb.models`, which doesn't exist; the full validate happens at Task 9. For now, temporarily comment out the `provider_url` line in `locals.tf`. Then run these, with the absolute path to terraform.exe if it is not on PATH yet (winget puts it under `%LOCALAPPDATA%\Microsoft\WinGet\Links`):
  - `terraform -chdir=infra init -backend=false`
  - `terraform -chdir=infra fmt -recursive`
  - `terraform -chdir=infra validate`

  Expected: `Success! The configuration is valid.` If kreuzwerker/docker 4.x renamed the `build`/`triggers` arguments, adjust them to the provider docs (`terraform providers schema -json` shows the schema). Restore the `provider_url` line afterwards.

- [ ] **Step 12: Commit.** This includes `infra/.terraform.lock.hcl`, which pins the provider versions.
```bash
git add -A infra .gitignore .dockerignore && git commit -m "infra: network, gateway endpoints, ECR image build, DynamoDB table, least-privilege IAM, log groups"
```

---

### Task 8: Terraform: ALB, ECS service, autoscaling on ActiveCalls

**Files:**
- Create: `infra/alb.tf`, `infra/ecs.tf`, `infra/autoscaling.tf`
- Modify: `infra/outputs.tf`

**Interfaces:**
- Consumes: Task 7 resources; `local.provider_url` (Task 9's NLB); EMF names (Task 3).
- Produces: `aws_lb.app`, `aws_lb_target_group.app`, `aws_ecs_cluster.main`, `aws_ecs_service.app`, `aws_security_group.tasks` (used by Task 9's NLB rules); the output `url`.

- [ ] **Step 1: `infra/alb.tf`:**

```hcl
resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "Public ALB: HTTP from allowed CIDRs only"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${local.name}-alb" }
}

resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  for_each          = toset(var.allowed_cidrs)
  security_group_id = aws_security_group.alb.id
  cidr_ipv4         = each.value
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

resource "aws_vpc_security_group_egress_rule" "alb_to_tasks" {
  security_group_id            = aws_security_group.alb.id
  referenced_security_group_id = aws_security_group.tasks.id
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
}

resource "aws_lb" "app" {
  name                       = local.name
  load_balancer_type         = "application"
  internal                   = false
  subnets                    = aws_subnet.public[*].id
  security_groups            = [aws_security_group.alb.id]
  drop_invalid_header_fields = true
  idle_timeout               = 60
}

resource "aws_lb_target_group" "app" {
  name                 = "${local.name}-app"
  vpc_id               = aws_vpc.main.id
  target_type          = "ip"
  port                 = 8080
  protocol             = "HTTP"
  deregistration_delay = 30
  health_check {
    path                = "/health"
    matcher             = "200"
    interval            = 10
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
  # One call = one task: the voice session lives on the task that took its first turn.
  stickiness {
    type            = "lb_cookie"
    cookie_duration = 300
    enabled         = true
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.app.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}
```

- [ ] **Step 2: `infra/ecs.tf`:**

```hcl
resource "aws_security_group" "tasks" {
  name        = "${local.name}-tasks"
  description = "voice-agent tasks: 8080 from the ALB only"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${local.name}-tasks" }
}

resource "aws_vpc_security_group_ingress_rule" "tasks_from_alb" {
  security_group_id            = aws_security_group.tasks.id
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
}

resource "aws_vpc_security_group_egress_rule" "tasks_out" {
  security_group_id = aws_security_group.tasks.id
  cidr_ipv4         = "0.0.0.0/0" # NLB (model tier), DynamoDB endpoint, ECR via NAT
  ip_protocol       = "-1"
}

resource "aws_ecs_cluster" "main" {
  name = local.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

locals {
  provider_kind = var.model_tier == "gpu" ? "openai" : "mock"
  app_env = {
    HOST             = "0.0.0.0"
    PORT             = "8080"
    LOG_LEVEL        = "INFO"
    STORE_BACKEND    = "dynamodb"
    DYNAMODB_TABLE   = aws_dynamodb_table.state.name
    AWS_REGION       = var.region
    EMF_ENABLED      = "true"
    EMF_NAMESPACE    = local.metric_namespace
    EMF_SERVICE_NAME = local.service_name
    TURN_DEADLINE_S  = tostring(var.turn_deadline_s)
    STT_PROVIDER     = local.provider_kind
    STT_BASE_URL     = local.provider_url
    STT_MODEL        = var.stt_model
    STT_TIMEOUT_S    = tostring(var.stage_timeouts_s.stt)
    LLM_PROVIDER     = local.provider_kind
    LLM_BASE_URL     = local.provider_url
    LLM_MODEL        = var.llm_model
    LLM_TIMEOUT_S    = tostring(var.stage_timeouts_s.llm)
    TTS_PROVIDER     = local.provider_kind
    TTS_BASE_URL     = local.provider_url
    TTS_MODEL        = var.tts_model
    TTS_VOICE        = var.tts_voice
    TTS_TIMEOUT_S    = tostring(var.stage_timeouts_s.tts)
  }
}

resource "aws_ecs_task_definition" "app" {
  family                   = local.service_name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  container_definitions = jsonencode([{
    name         = local.service_name
    image        = local.app_image
    essential    = true
    command      = ["voice-agent", "serve"]
    portMappings = [{ containerPort = 8080, protocol = "tcp" }]
    environment  = [for k, v in local.app_env : { name = k, value = v }]
    stopTimeout  = 30
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app.name
        awslogs-region        = var.region
        awslogs-stream-prefix = local.service_name
      }
    }
  }])
}

resource "aws_ecs_service" "app" {
  name                               = local.service_name
  cluster                            = aws_ecs_cluster.main.id
  task_definition                    = aws_ecs_task_definition.app.arn
  desired_count                      = var.min_tasks
  launch_type                        = "FARGATE"
  health_check_grace_period_seconds  = 30
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  availability_zone_rebalancing      = "ENABLED"
  propagate_tags                     = "SERVICE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = local.service_name
    container_port   = 8080
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  lifecycle {
    ignore_changes = [desired_count] # owned by autoscaling after creation
  }

  depends_on = [aws_lb_listener.http]
}
```

- [ ] **Step 3: `infra/autoscaling.tf`:**

```hcl
resource "aws_appautoscaling_target" "app" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.app.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.min_tasks
  max_capacity       = var.max_tasks
}

# Scale on what a voice task actually runs out of: concurrent calls, not CPU.
# ActiveCalls is emitted per task with only the ServiceName dimension, so its Average
# is "calls per task": double the tasks, halve the value.
resource "aws_appautoscaling_policy" "active_calls" {
  name               = "active-calls-per-task"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.app.service_namespace
  resource_id        = aws_appautoscaling_target.app.resource_id
  scalable_dimension = aws_appautoscaling_target.app.scalable_dimension

  target_tracking_scaling_policy_configuration {
    target_value       = var.target_active_calls
    scale_out_cooldown = 60
    scale_in_cooldown  = 180
    customized_metric_specification {
      metric_name = "ActiveCalls"
      namespace   = local.metric_namespace
      statistic   = "Average"
      dimensions {
        name  = "ServiceName"
        value = local.service_name
      }
    }
  }
}

# Campaign windows are known in advance: pre-warm instead of waiting for the ramp.
resource "aws_appautoscaling_scheduled_action" "campaign_start" {
  count              = var.campaign_prewarm == null ? 0 : 1
  name               = "campaign-prewarm"
  service_namespace  = aws_appautoscaling_target.app.service_namespace
  resource_id        = aws_appautoscaling_target.app.resource_id
  scalable_dimension = aws_appautoscaling_target.app.scalable_dimension
  schedule           = var.campaign_prewarm.start
  scalable_target_action {
    min_capacity = var.campaign_prewarm.min_tasks
    max_capacity = var.max_tasks
  }
}

resource "aws_appautoscaling_scheduled_action" "campaign_end" {
  count              = var.campaign_prewarm == null ? 0 : 1
  name               = "campaign-end"
  service_namespace  = aws_appautoscaling_target.app.service_namespace
  resource_id        = aws_appautoscaling_target.app.resource_id
  scalable_dimension = aws_appautoscaling_target.app.scalable_dimension
  schedule           = var.campaign_prewarm.end
  scalable_target_action {
    min_capacity = var.min_tasks
    max_capacity = var.max_tasks
  }
  depends_on = [aws_appautoscaling_scheduled_action.campaign_start]
}
```

- [ ] **Step 4: Outputs.** Append:

```hcl
output "url" {
  value = "http://${aws_lb.app.dns_name}"
}

output "cluster" {
  value = aws_ecs_cluster.main.name
}
```

- [ ] **Step 5: Format and validate after Task 9** (the NLB that `provider_url` needs comes in Task 9). Commit now:
```bash
terraform -chdir=infra fmt -recursive
git add -A infra && git commit -m "infra: public ALB with stickiness, Fargate service in private subnets, target tracking on ActiveCalls, scheduled pre-warm"
```

---

### Task 9: Terraform: model tier (internal NLB → mock service or GPU ASG)

**Files:**
- Create: `infra/models.tf`, `infra/gpu.tf`, `infra/templates/gpu-user-data.sh.tftpl`, `infra/templates/compose.yaml.tftpl`
- Modify: `infra/outputs.tf`

**Interfaces:**
- Consumes: `aws_security_group.tasks`, the private subnets, `local.app_image`, the GPU instance profile, `aws_cloudwatch_log_group.gpu`.
- Produces:
  - `aws_lb.models` (internal NLB, port 8080);
  - the GPU ASG `collectionsinference-gpu`, whose instances are tagged `Name=collectionsinference-gpu` (`scripts/chaos.py` relies on this tag).

- [ ] **Step 1: `infra/models.tf`:**

```hcl
resource "aws_security_group" "nlb" {
  name        = "${local.name}-models-nlb"
  description = "Internal NLB to the model tier: 8080 from voice-agent tasks only"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${local.name}-models-nlb" }
}

resource "aws_vpc_security_group_ingress_rule" "nlb_from_tasks" {
  security_group_id            = aws_security_group.nlb.id
  referenced_security_group_id = aws_security_group.tasks.id
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
}

resource "aws_vpc_security_group_egress_rule" "nlb_to_models" {
  security_group_id            = aws_security_group.nlb.id
  referenced_security_group_id = aws_security_group.models.id
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
}

resource "aws_security_group" "models" {
  name        = "${local.name}-models"
  description = "Model tier (mock task or GPU host): 8080 from the NLB only"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${local.name}-models" }
}

resource "aws_vpc_security_group_ingress_rule" "models_from_nlb" {
  security_group_id            = aws_security_group.models.id
  referenced_security_group_id = aws_security_group.nlb.id
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
}

resource "aws_vpc_security_group_egress_rule" "models_out" {
  security_group_id = aws_security_group.models.id
  cidr_ipv4         = "0.0.0.0/0" # image and model downloads via NAT
  ip_protocol       = "-1"
}

resource "aws_lb" "models" {
  name                             = "${local.name}-models"
  load_balancer_type               = "network"
  internal                         = true
  subnets                          = aws_subnet.private[*].id
  security_groups                  = [aws_security_group.nlb.id]
  enable_cross_zone_load_balancing = true # one GPU host serves tasks in both AZs
}

resource "aws_lb_target_group" "models" {
  name                 = "${local.name}-models-${var.model_tier}"
  vpc_id               = aws_vpc.main.id
  target_type          = var.model_tier == "gpu" ? "instance" : "ip"
  port                 = 8080
  protocol             = "TCP"
  deregistration_delay = 10
  health_check {
    protocol            = "HTTP"
    path                = var.model_tier == "gpu" ? "/ready" : "/health"
    port                = "8080"
    interval            = 10
    healthy_threshold   = 2
    unhealthy_threshold = 2
  }
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_lb_listener" "models" {
  load_balancer_arn = aws_lb.models.arn
  port              = 8080
  protocol          = "TCP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.models.arn
  }
}

# ---- model_tier = "mock": the Round 1 mock provider as a small Fargate service -------
resource "aws_ecs_task_definition" "mock" {
  count                    = var.model_tier == "mock" ? 1 : 0
  family                   = "mock-provider"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.execution.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  container_definitions = jsonencode([{
    name         = "mock-provider"
    image        = local.app_image
    essential    = true
    command      = ["voice-agent", "mock", "--host", "0.0.0.0", "--port", "8080"]
    portMappings = [{ containerPort = 8080, protocol = "tcp" }]
    environment  = [{ name = "MOCK_FAILURE_RATE", value = tostring(var.failure_rate) }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "mock-provider"
      }
    }
  }])
}

resource "aws_ecs_service" "mock" {
  count           = var.model_tier == "mock" ? 1 : 0
  name            = "mock-provider"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.mock[0].arn
  desired_count   = 1
  launch_type     = "FARGATE"
  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.models.id]
    assign_public_ip = false
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.models.arn
    container_name   = "mock-provider"
    container_port   = 8080
  }
  depends_on = [aws_lb_listener.models]
}
```

- [ ] **Step 2: `infra/gpu.tf`:**

```hcl
data "aws_ssm_parameter" "dlami" {
  name = "/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-amazon-linux-2023/latest/ami-id"
}

locals {
  registry = split("/", aws_ecr_repository.app.repository_url)[0]
  compose = templatefile("${path.module}/templates/compose.yaml.tftpl", {
    vllm_image     = var.vllm_image
    speaches_image = var.speaches_image
    proxy_image    = local.app_image
    llm_hf_model   = var.llm_hf_model
    llm_model      = var.llm_model
    stt_model      = var.stt_model
    tts_model      = var.tts_model
    failure_rate   = var.failure_rate
    log_group      = aws_cloudwatch_log_group.gpu.name
    region         = var.region
  })
}

resource "aws_launch_template" "gpu" {
  count                  = var.model_tier == "gpu" ? 1 : 0
  name_prefix            = "${local.name}-gpu-"
  image_id               = data.aws_ssm_parameter.dlami.insecure_value
  instance_type          = var.gpu_instance_type
  vpc_security_group_ids = [aws_security_group.models.id]
  iam_instance_profile {
    arn = aws_iam_instance_profile.gpu.arn
  }
  metadata_options {
    http_tokens                 = "required" # IMDSv2 only
    http_put_response_hop_limit = 1
  }
  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = 100
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }
  user_data = base64encode(templatefile("${path.module}/templates/gpu-user-data.sh.tftpl", {
    compose         = local.compose
    region          = var.region
    registry        = local.registry
    compose_version = var.compose_version
  }))
  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "${local.name}-gpu" }
  }
  tag_specifications {
    resource_type = "volume"
    tags          = { Name = "${local.name}-gpu" }
  }
}

resource "aws_autoscaling_group" "gpu" {
  count                     = var.model_tier == "gpu" ? 1 : 0
  name                      = "${local.name}-gpu"
  min_size                  = 1
  max_size                  = 1
  desired_capacity          = 1
  vpc_zone_identifier       = aws_subnet.private[*].id # both subnets are in g5 AZs
  target_group_arns         = [aws_lb_target_group.models.arn]
  health_check_type         = "EC2" # don't kill the host while 18 GB of models load
  health_check_grace_period = 1200
  launch_template {
    id      = aws_launch_template.gpu[0].id
    version = aws_launch_template.gpu[0].latest_version
  }
  instance_refresh {
    strategy = "Rolling"
    preferences {
      min_healthy_percentage = 0
    }
  }
  tag {
    key                 = "Name"
    value               = "${local.name}-gpu"
    propagate_at_launch = true
  }
}

# Dead-man switch: the GPU scales to zero gpu_max_hours after creation, even if nobody
# remembers to destroy. Extend with: terraform apply -replace='aws_autoscaling_schedule.gpu_off[0]'
resource "aws_autoscaling_schedule" "gpu_off" {
  count                  = var.model_tier == "gpu" ? 1 : 0
  scheduled_action_name  = "dead-man-switch"
  autoscaling_group_name = aws_autoscaling_group.gpu[0].name
  start_time             = timeadd(plantimestamp(), "${var.gpu_max_hours}h")
  min_size               = 0
  max_size               = 0
  desired_capacity       = 0
  lifecycle {
    ignore_changes = [start_time]
  }
}
```

- [ ] **Step 3: `infra/templates/gpu-user-data.sh.tftpl`.** Only `${compose}`, `${region}`, `${registry}` and `${compose_version}` are template variables. Shell `$(...)` is untouched by `templatefile`.

```bash
#!/bin/bash
# GPU host bootstrap: vLLM + Speaches + chaos proxy under docker compose.
set -euo pipefail
exec > >(tee -a /var/log/collections-bootstrap.log) 2>&1
echo "bootstrap start $(date -Is)"

systemctl enable --now docker
if ! docker info 2>/dev/null | grep -qi nvidia; then
  nvidia-ctk runtime configure --runtime=docker
  systemctl restart docker
fi
if ! docker compose version >/dev/null 2>&1; then
  mkdir -p /usr/local/lib/docker/cli-plugins
  curl -fsSL "https://github.com/docker/compose/releases/download/${compose_version}/docker-compose-linux-x86_64" \
    -o /usr/local/lib/docker/cli-plugins/docker-compose
  chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
fi

mkdir -p /opt/collections /opt/hf/vllm /opt/hf/speaches
chmod 777 /opt/hf/speaches   # speaches runs as a non-root user
cat > /opt/collections/compose.yaml <<'COMPOSE'
${compose}
COMPOSE

aws ecr get-login-password --region ${region} | docker login --username AWS --password-stdin ${registry}
cd /opt/collections
docker compose pull --quiet
docker compose up -d
nvidia-smi || true

for i in $(seq 1 180); do
  if curl -fsS localhost:8080/ready >/dev/null 2>&1; then
    echo "models ready after $((i * 10))s"
    nvidia-smi --query-gpu=memory.used,memory.total --format=csv
    exit 0
  fi
  sleep 10
done
echo "models NOT ready after 30 min"
docker compose ps
exit 1
```

- [ ] **Step 4: `infra/templates/compose.yaml.tftpl`:**

```yaml
# One A10G shared by three containers. VRAM budget (~22.5 GiB usable):
# vLLM 70% (~15.7 GiB: 5.5 GB AWQ weights + KV cache), Speaches the rest.
x-gpu: &gpu
  deploy:
    resources:
      reservations:
        devices:
          - driver: nvidia
            device_ids: ["0"]
            capabilities: [gpu]

services:
  vllm:
    image: ${vllm_image}
    <<: *gpu
    ipc: host
    restart: unless-stopped
    command:
      - --model
      - ${llm_hf_model}
      - --served-model-name
      - ${llm_model}
      - --quantization
      - awq_marlin
      - --gpu-memory-utilization
      - "0.70"
      - --max-model-len
      - "8192"
      - --max-num-seqs
      - "16"
      - --port
      - "8000"
    volumes:
      - /opt/hf/vllm:/root/.cache/huggingface
    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"]
      interval: 15s
      timeout: 5s
      retries: 120
      start_period: 60s
    logging:
      driver: awslogs
      options: { awslogs-group: "${log_group}", awslogs-region: "${region}", awslogs-stream: vllm }

  speaches:
    image: ${speaches_image}
    <<: *gpu
    restart: unless-stopped
    depends_on:
      vllm: { condition: service_healthy } # vLLM sizes its cache from free VRAM: start it first
    environment:
      PRELOAD_MODELS: '["${stt_model}","${tts_model}"]'
      STT_MODEL_TTL: "-1"
      TTS_MODEL_TTL: "-1"
      WHISPER__INFERENCE_DEVICE: cuda
      WHISPER__COMPUTE_TYPE: float16
    volumes:
      - /opt/hf/speaches:/home/ubuntu/.cache/huggingface/hub
    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"]
      interval: 15s
      timeout: 5s
      retries: 80
      start_period: 30s
    logging:
      driver: awslogs
      options: { awslogs-group: "${log_group}", awslogs-region: "${region}", awslogs-stream: speaches }

  chaos-proxy:
    image: ${proxy_image}
    restart: unless-stopped
    command: ["voice-agent", "chaos-proxy", "--host", "0.0.0.0", "--port", "8080"]
    environment:
      PROXY_LLM_UPSTREAM: http://vllm:8000
      PROXY_AUDIO_UPSTREAM: http://speaches:8000
      MOCK_FAILURE_RATE: "${failure_rate}"
    ports:
      - "8080:8080"
    depends_on:
      vllm: { condition: service_started }
      speaches: { condition: service_started }
    logging:
      driver: awslogs
      options: { awslogs-group: "${log_group}", awslogs-region: "${region}", awslogs-stream: chaos-proxy }
```

- [ ] **Step 5: Outputs.** Append:

```hcl
output "models_endpoint" {
  value = local.provider_url
}

output "gpu_asg" {
  value = var.model_tier == "gpu" ? aws_autoscaling_group.gpu[0].name : null
}
```

- [ ] **Step 6: Validate the whole config.**
  - `terraform -chdir=infra fmt -recursive && terraform -chdir=infra validate`. Expected: valid.
  - Check that the compose template renders as YAML: `terraform -chdir=infra console` → `local.compose` is not available without providers configured. So instead render with Python: `uv run python -c "import yaml"` is not a dependency. Skip, and rely on the GPU boot logs in Task 13.

- [ ] **Step 7: Commit.**
```bash
git add -A infra && git commit -m "infra: internal NLB model tier (mock Fargate or g5.xlarge ASG with vLLM + Speaches + chaos proxy) and GPU dead-man switch"
```

---

### Task 10: Terraform: dashboard, alarms, scaling and task events

**Files:**
- Create: `infra/observability.tf`
- Modify: `infra/outputs.tf`

**Interfaces:**
- Consumes: EMF metric names (Task 3), `aws_lb.app`, `aws_lb_target_group.app`, `aws_ecs_cluster.main`, `aws_ecs_service.app`, `aws_lb_target_group.models`, `aws_lb.models`.
- Produces: the dashboard `collectionsinference`, the alarms, and the log group `/aws/events/collectionsinference`.

- [ ] **Step 1: `infra/observability.tf`:**

```hcl
locals {
  svc  = ["ServiceName", local.service_name]
  ns   = local.metric_namespace
  alb  = aws_lb.app.arn_suffix
  tg   = aws_lb_target_group.app.arn_suffix
  ecsd = ["ClusterName", aws_ecs_cluster.main.name, "ServiceName", aws_ecs_service.app.name]
  stage_metric = { for s in ["stt", "llm", "tts"] : s => ["ServiceName", local.service_name, "Stage", s] }
}

# ---- ECS task / service events -> logs (autoscaling activity on the dashboard) -------
resource "aws_cloudwatch_log_group" "events" {
  name              = "/aws/events/${local.name}"
  retention_in_days = var.log_retention_days
}

data "aws_iam_policy_document" "events_to_logs" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.events.arn}:*"]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com", "delivery.logs.amazonaws.com"]
    }
  }
}

resource "aws_cloudwatch_log_resource_policy" "events" {
  policy_name     = "${local.name}-events"
  policy_document = data.aws_iam_policy_document.events_to_logs.json
}

resource "aws_cloudwatch_event_rule" "ecs" {
  name = "${local.name}-ecs-events"
  event_pattern = jsonencode({
    source      = ["aws.ecs"]
    detail-type = ["ECS Task State Change", "ECS Service Action", "ECS Deployment State Change"]
    detail      = { clusterArn = [aws_ecs_cluster.main.arn] }
  })
}

resource "aws_cloudwatch_event_target" "ecs_to_logs" {
  rule = aws_cloudwatch_event_rule.ecs.name
  arn  = aws_cloudwatch_log_group.events.arn
}

# ---- Alarms ---------------------------------------------------------------------------
resource "aws_sns_topic" "alarms" {
  name = "${local.name}-alarms"
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alert_email == null ? 0 : 1
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_cloudwatch_metric_alarm" "p99_latency" {
  alarm_name          = "${local.name}-p99-latency-3x-baseline"
  alarm_description   = "RUNBOOK trigger: p99 turn latency > 3x baseline. See RUNBOOK.md."
  namespace           = local.ns
  metric_name         = "TurnLatency"
  dimensions          = { ServiceName = local.service_name }
  extended_statistic  = "p99"
  period              = 60
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  comparison_operator = "GreaterThanThreshold"
  threshold           = 3 * var.baseline_p99_ms
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "target_5xx" {
  alarm_name          = "${local.name}-http-5xx"
  alarm_description   = "The service itself returned 5xx (degraded turns are 200s; this should stay 0)."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  dimensions          = { LoadBalancer = local.alb, TargetGroup = local.tg }
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 2
  comparison_operator = "GreaterThanThreshold"
  threshold           = 5
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "breaker_open" {
  for_each            = local.stage_metric
  alarm_name          = "${local.name}-breaker-open-${each.key}"
  alarm_description   = "The ${each.key} circuit breaker is open on at least one task: dependency outage."
  namespace           = local.ns
  metric_name         = "BreakerState"
  dimensions          = { ServiceName = local.service_name, Stage = each.key }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 2
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "dlq_pending" {
  alarm_name          = "${local.name}-dlq-pending"
  alarm_description   = "Dead letters are piling up: replay once the dependency recovers (RUNBOOK.md)."
  namespace           = local.ns
  metric_name         = "DlqPending"
  dimensions          = { ServiceName = local.service_name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 5
  comparison_operator = "GreaterThanThreshold"
  threshold           = 50
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}

# ---- Dashboard ------------------------------------------------------------------------
locals {
  widgets = [
    {
      type = "text", x = 0, y = 0, width = 24, height = 2
      properties = { markdown = "## Collections voice-agent (ap-south-1)\nTraffic, capacity, dependencies. Runbook trigger: p99 turn latency > 3x baseline (${3 * var.baseline_p99_ms} ms). Degraded turns are answered with a fallback prompt and dead-lettered; the replay path is in RUNBOOK.md." }
    },
    {
      type = "metric", x = 0, y = 2, width = 8, height = 6
      properties = {
        title = "Request rate (turns/min)", region = var.region, view = "timeSeries", stat = "Sum", period = 60
        metrics = [
          [local.ns, "Turns", local.svc[0], local.svc[1], { label = "turns" }],
          ["AWS/ApplicationELB", "RequestCount", "LoadBalancer", local.alb, { label = "ALB requests" }],
        ]
      }
    },
    {
      type = "metric", x = 8, y = 2, width = 8, height = 6
      properties = {
        title = "Error rate (%)", region = var.region, view = "timeSeries", period = 60, yAxis = { left = { min = 0 } }
        metrics = [
          [{ expression = "100 * IF(m1 > 0, m2 / m1, 0)", label = "degraded turns %", id = "e1" }],
          [{ expression = "100 * IF(m3 > 0, m4 / m3, 0)", label = "HTTP 5xx %", id = "e2" }],
          [local.ns, "Turns", local.svc[0], local.svc[1], { id = "m1", stat = "Sum", visible = false }],
          [local.ns, "TurnsDegraded", local.svc[0], local.svc[1], { id = "m2", stat = "Sum", visible = false }],
          ["AWS/ApplicationELB", "RequestCount", "LoadBalancer", local.alb, { id = "m3", stat = "Sum", visible = false }],
          ["AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", "LoadBalancer", local.alb, { id = "m4", stat = "Sum", visible = false }],
        ]
      }
    },
    {
      type = "metric", x = 16, y = 2, width = 8, height = 6
      properties = {
        title = "Turn latency percentiles (ms)", region = var.region, view = "timeSeries", period = 60
        metrics = [
          [local.ns, "TurnLatency", local.svc[0], local.svc[1], { stat = "p50", label = "p50" }],
          [local.ns, "TurnLatency", local.svc[0], local.svc[1], { stat = "p90", label = "p90" }],
          [local.ns, "TurnLatency", local.svc[0], local.svc[1], { stat = "p99", label = "p99" }],
        ]
        annotations = { horizontal = [{ label = "3x baseline p99", value = 3 * var.baseline_p99_ms }] }
      }
    },
    {
      type = "metric", x = 0, y = 8, width = 8, height = 6
      properties = {
        title = "Active calls (scaling signal)", region = var.region, view = "timeSeries", period = 60
        metrics = [
          [local.ns, "ActiveCalls", local.svc[0], local.svc[1], { stat = "Average", label = "per task (target ${var.target_active_calls})" }],
          [local.ns, "ActiveCalls", local.svc[0], local.svc[1], { stat = "Sum", label = "sum of samples", visible = false }],
          [local.ns, "InFlightTurns", local.svc[0], local.svc[1], { stat = "Average", label = "in-flight turns per task" }],
        ]
        annotations = { horizontal = [{ label = "target", value = var.target_active_calls }] }
      }
    },
    {
      type = "metric", x = 8, y = 8, width = 8, height = 6
      properties = {
        title = "Tasks: desired vs running (autoscaling)", region = var.region, view = "timeSeries", stat = "Average", period = 60
        metrics = [
          ["ECS/ContainerInsights", "DesiredTaskCount", local.ecsd[0], local.ecsd[1], local.ecsd[2], local.ecsd[3], { label = "desired" }],
          ["ECS/ContainerInsights", "RunningTaskCount", local.ecsd[0], local.ecsd[1], local.ecsd[2], local.ecsd[3], { label = "running" }],
        ]
      }
    },
    {
      type = "metric", x = 16, y = 8, width = 8, height = 6
      properties = {
        title = "Task CPU / memory (%)", region = var.region, view = "timeSeries", stat = "Average", period = 60
        metrics = [
          ["AWS/ECS", "CPUUtilization", local.ecsd[0], local.ecsd[1], local.ecsd[2], local.ecsd[3]],
          ["AWS/ECS", "MemoryUtilization", local.ecsd[0], local.ecsd[1], local.ecsd[2], local.ecsd[3]],
        ]
      }
    },
    {
      type = "metric", x = 0, y = 14, width = 8, height = 6
      properties = {
        title = "Provider failures by stage (attempts)", region = var.region, view = "timeSeries", stat = "Sum", period = 60
        metrics = concat(
          [for s, d in local.stage_metric : [local.ns, "ProviderFailures", d[0], d[1], d[2], d[3], { label = "${s} failures" }]],
          [for s, d in local.stage_metric : [local.ns, "Retries", d[0], d[1], d[2], d[3], { label = "${s} retries" }]],
        )
      }
    },
    {
      type = "metric", x = 8, y = 14, width = 8, height = 6
      properties = {
        title = "Circuit breakers (0 closed, 1 half-open, 2 open)", region = var.region, view = "timeSeries", stat = "Maximum", period = 60
        yAxis = { left = { min = 0, max = 2 } }
        metrics = [for s, d in local.stage_metric : [local.ns, "BreakerState", d[0], d[1], d[2], d[3], { label = s }]]
      }
    },
    {
      type = "metric", x = 16, y = 14, width = 8, height = 6
      properties = {
        title = "Dead-letter queue", region = var.region, view = "timeSeries", period = 60
        metrics = [
          [local.ns, "DlqPending", local.svc[0], local.svc[1], { stat = "Maximum", label = "pending" }],
          [local.ns, "DeadLetters", local.svc[0], local.svc[1], { stat = "Sum", label = "new per min" }],
          [local.ns, "ReplaysResolved", local.svc[0], local.svc[1], { stat = "Sum", label = "resolved by replay" }],
        ]
      }
    },
    {
      type = "metric", x = 0, y = 20, width = 8, height = 6
      properties = {
        title = "Model tier health (NLB healthy hosts)", region = var.region, view = "timeSeries", stat = "Minimum", period = 60
        metrics = [["AWS/NetworkELB", "HealthyHostCount", "TargetGroup", aws_lb_target_group.models.arn_suffix, "LoadBalancer", aws_lb.models.arn_suffix]]
      }
    },
    {
      type = "log", x = 8, y = 20, width = 16, height = 6
      properties = {
        title  = "Autoscaling and task events"
        region = var.region
        view   = "table"
        query  = "SOURCE '${aws_cloudwatch_log_group.events.name}' | fields @timestamp, `detail-type`, detail.group, detail.lastStatus, detail.eventName, detail.desiredCount | sort @timestamp desc | limit 50"
      }
    },
  ]
}

resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = local.name
  dashboard_body = jsonencode({ widgets = local.widgets })
}
```

- [ ] **Step 2: Outputs.** Append:

```hcl
output "dashboard_url" {
  value = "https://${var.region}.console.aws.amazon.com/cloudwatch/home?region=${var.region}#dashboards/dashboard/${aws_cloudwatch_dashboard.main.dashboard_name}"
}
```

- [ ] **Step 3: Format, validate and commit.**
```bash
terraform -chdir=infra fmt -recursive && terraform -chdir=infra validate
git add -A infra && git commit -m "infra: CloudWatch dashboard, runbook alarms, ECS events to logs"
```

---

### Task 11: CI and documentation

**Files:**
- Modify: `.github/workflows/ci.yml`, `README.md`
- Create: `RUNBOOK.md`, `docs/demo-shot-list.md`, `docs/ap-south-1-notes.md`

- [ ] **Step 1: CI.** Add a job to `.github/workflows/ci.yml`:

```yaml
  terraform:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: hashicorp/setup-terraform@v3
      - run: terraform -chdir=infra fmt -check -recursive
      - run: terraform -chdir=infra init -backend=false
      - run: terraform -chdir=infra validate
```

- [ ] **Step 2: `RUNBOOK.md`.** Follow Appendix I's headings **exactly**:
  - `# Runbook: Collections Voice-Agent Inference Service`
  - `## Trigger`
  - `## First 5 minutes`
  - `## If downstream is the cause`
  - `## If autoscaling is the cause`
  - `## Escalation`
  - `## Post-incident`

  Each step is concrete, using the real names from this system:
  - **Trigger:** the alarm `collectionsinference-p99-latency-3x-baseline` (p99 `TurnLatency` > 3 × `baseline_p99_ms`, 3 of 3 minutes) and how it pages (the SNS topic `collectionsinference-alarms`).
  - **First 5 minutes:** the three Appendix I checks, each mapped to a dashboard widget title from Task 10 and a CLI fallback:
    - `aws ecs describe-services --cluster collectionsinference --services voice-agent --profile predixion --query 'services[0].[desiredCount,runningCount,pendingCount]'`;
    - `aws application-autoscaling describe-scaling-activities --service-namespace ecs --resource-id service/collectionsinference/voice-agent --profile predixion --max-items 10`;
    - `curl $URL/health` for breaker states;
    - `uv run python scripts/chaos.py stats` for what the model tier actually received.
  - **If downstream is the cause:**
    - The breaker should trip at ≥ 60% failures over its last 50 calls (minimum 20). If it hasn't, check the `BreakerState` widget and `BREAKER_*` env.
    - DLQ depth: `curl $URL/v1/dlq`, the `DlqPending` widget, and `STORE_BACKEND=dynamodb AWS_PROFILE=predixion uv run voice-agent dlq list`.
    - Every degraded turn has a dead letter by construction: a single TransactWriteItems.
  - **If autoscaling is the cause:**
    - Desired vs running.
    - Fargate quota: `aws service-quotas get-service-quota --service-code fargate --quota-code L-3032A538 --profile predixion`.
    - Subnet IP space: the private /23 subnets hold about 500 IPs each.
    - Cooldowns: scale-out 60 s, scale-in 180 s, target 10 calls per task.
    - Pre-warm through `campaign_prewarm`.
  - **Escalation:**
    - Primary on-call: the deploying engineer. Fill in a name and phone, and say who is the backup.
    - After 15 minutes unresolved in a live campaign window, page the platform lead.
    - After 30 minutes, pause the campaign dialer (stop new calls) and switch to human agents.
  - **Post-incident:**
    - Replay: `curl -X POST "$URL/v1/dlq/replay?limit=1000"`. It is canary-first, refused (HTTP 503) while any breaker is open, and safe to repeat.
    - Record actual vs expected numbers in a table.
    - Teardown reminder.

- [ ] **Step 3: `docs/demo-shot-list.md`.** A 5-minute recording plan with timestamps:

  | Time | Shot |
  |---|---|
  | 0:00–0:30 | architecture slide, `terraform apply` output |
  | 0:30–1:15 | dashboard at baseline: flat 2 tasks, ActiveCalls ~5/task |
  | 1:15–2:30 | spike phase: ActiveCalls rises, desired → running tasks climb, latency percentiles hold |
  | 2:30–3:30 | `scripts/chaos.py outage --stage llm --seconds 60`: breaker widget → 2, degraded % rises, HTTP 5xx stays 0, DLQ pending climbs |
  | 3:30–4:15 | recovery: breakers close; `POST /v1/dlq/replay`; DLQ resolves |
  | 4:15–5:00 | campaign summary table (0 HTTP failures), `terraform destroy`, `teardown_check.py` CLEAN log |

  Also include the exact commands for each shot.

- [ ] **Step 4: `docs/ap-south-1-notes.md`.** About one page, covering:
  - borrower PII residency:
    - DPDP Act 2023;
    - RBI's April 2018 directive that payment-system data is stored only in India;
    - DynamoDB, CloudWatch Logs, ECR and the models all live in ap-south-1;
    - no third-party model API, so transcripts never leave the region;
  - logs contain transcripts: retention is 7 days and access is IAM-scoped. For production, mask PAN/Aadhaar before logging;
  - latency: Mumbai to Indian metros is roughly 10–40 ms RTT. Verify with your own `curl -w` numbers from Task 13 rather than quoting vendor figures;
  - g5 availability in only two AZs (aps1-az1/az3), and why subnets are chosen by AZ ID;
  - ap-south-2 (Hyderabad) as an in-country DR region;
  - in-region pricing: ap-south-1 Fargate is about 5% above us-east-1.

- [ ] **Step 5: README.** Add a "Round 2: AWS deployment (ap-south-1)" section after the Round 1 content:
  - an architecture diagram (from the spec);
  - prerequisites:
    - Free-plan account, and the organization SCP lifted (if the account sits in an AWS Organization);
    - G-instance quota ≥ 4 vCPUs (Appendix G);
    - Docker Desktop, Terraform ≥ 1.9, AWS CLI v2 and uv;
  - one-time setup: the `predixion` profile, the budget command used (the `aws budgets create-budget` with `IncludeCredit=false`) and `terraform.tfvars`;
  - deploy steps: `make_utterances.ps1` → `terraform init` → `apply` with mock tier → smoke → `model_tier = "gpu"` → `apply` → campaign → chaos → replay → `destroy` → `teardown_check.py`;
  - the IAM check command;
  - a cost table, with the estimate now and measured numbers filled in by Task 14;
  - the design decisions in short (from the spec);
  - deliverables mapping, including `RUNBOOK.md`, `docs/demo-shot-list.md`, `docs/ap-south-1-notes.md` and `teardown/*.log`;
  - an updated test count.

- [ ] **Step 6: Commit.**
```bash
git add -A && git commit -m "docs: Round 2 README, runbook, demo shot list, ap-south-1 notes; CI validates Terraform"
```

---

### Task 12 (live, operator): first deploy with `model_tier = "mock"`

Run by the controller session, not a subagent, because it touches the user's AWS account. Prerequisite: the organization SCP `p-h1se51u9` no longer denies EC2/ECS/etc. Check with `aws ec2 describe-vpcs --profile predixion --region ap-south-1`, which must succeed.

- [ ] **Step 1: Request the GPU quota right away** (approval can take hours):
```bash
aws service-quotas request-service-quota-increase --service-code ec2 --quota-code L-DB2E81BA --desired-value 4 --profile predixion --region ap-south-1
```
- [ ] **Step 2: Start Docker Desktop** and confirm with `docker info`. Write `infra/terraform.tfvars` from the example with the caller's IP /32 and `model_tier = "mock"`.
- [ ] **Step 3: Plan and check IAM.**
```bash
terraform -chdir=infra init
terraform -chdir=infra plan -out=tfplan
terraform -chdir=infra show -json tfplan > infra/plan.json
uv run python scripts/check_iam.py infra/plan.json
```
Expected: `IAM check: no wildcard actions`.
- [ ] **Step 4: Apply.** `terraform -chdir=infra apply tfplan`, about 5–8 minutes. Then:
  - `curl $(terraform -chdir=infra output -raw url)/health` returns `status ok` and three closed breakers;
  - re-run the IAM check against the state: `terraform -chdir=infra show -json > infra/plan.json && uv run python scripts/check_iam.py infra/plan.json`.
- [ ] **Step 5: Run a compressed campaign against the mock tier.**
```bash
uv run voice-agent campaign --url $(terraform -chdir=infra output -raw url) --profile baseline:3:10,spike:5:50,cooldown:3:10 --gap 20 --json-out results/mock-rehearsal.json
```
Expected:
  - 0 HTTP failures;
  - the degraded share stays small (single digits %);
  - ActiveCalls appears in CloudWatch within about 2 minutes;
  - desired tasks go above 2 during the spike and return after cool-down.

  Check the dashboard: every widget shows data except NLB healthy hosts.
- [ ] **Step 6: Fix what's broken, re-apply and commit the fixes.**
- [ ] **Step 7: Destroy if the GPU quota isn't approved yet.** Run `terraform -chdir=infra destroy`, then `uv run python scripts/teardown_check.py`. Expected: CLEAN.

### Task 13 (live, operator): GPU tier, measurement, recorded demo

- [ ] **Step 1: Confirm the quota.** `aws service-quotas get-service-quota --service-code ec2 --quota-code L-DB2E81BA --profile predixion --region ap-south-1` shows a value ≥ 4.
- [ ] **Step 2: Bring up the GPU.** Set `model_tier = "gpu"` and apply. Watch the bootstrap from the `/gpu/collectionsinference` log group, or with `aws ssm start-session --target <id> --profile predixion` then `tail -f /var/log/collections-bootstrap.log`. Expected: `models ready after Ns` and `nvidia-smi` memory use under 22 GB.
- [ ] **Step 3: Measure at 0% injection.** Run `uv run python scripts/chaos.py rate --rate 0`, then a 3-minute baseline campaign. Record per-stage latency (from `/admin/stats` timing and the campaign p50/p99). Set:
  - `stage_timeouts_s` to about 2 × the measured p99 per stage;
  - `turn_deadline_s` to the sum plus retry headroom;
  - `baseline_p99_ms` to the measured baseline p99.

  Then apply and restore `chaos.py rate --rate 0.2`.
- [ ] **Step 4: Rehearse.** Run the full campaign at the spec's profile (`baseline:10:10,spike:15:50,cooldown:10:10`, gap 30). At spike +5 min, run `scripts/chaos.py outage --stage llm --seconds 60`. After the run, `curl -X POST "$URL/v1/dlq/replay?limit=1000"`. Keep `results/campaign.json`.
- [ ] **Step 5: Multi-AZ stretch.** During the baseline, stop every task in one AZ:
  1. `aws ecs list-tasks --cluster collectionsinference --service-name voice-agent --profile predixion`;
  2. `aws ecs describe-tasks`, filtered by `availabilityZone`;
  3. `aws ecs stop-task` for each.

  Show that 0 HTTP failures appear beyond in-flight turns and that ECS replaces the tasks.
- [ ] **Step 6: Record the 5-minute demo** following `docs/demo-shot-list.md`.

### Task 14 (live, operator): teardown and measured cost write-up

- [ ] **Step 1: Destroy and prove it.**
  - `terraform -chdir=infra destroy`;
  - `uv run python scripts/teardown_check.py`, expecting `CLEAN`;
  - commit `teardown/teardown-check-*.log`;
  - take a screenshot of the EC2/ECS/VPC console pages showing them empty.
- [ ] **Step 2: Write `cost/measured.py`.** It uses `boto3.Session(profile_name="predixion").client("ce", region_name="us-east-1")` and calls `get_cost_and_usage`:
  - `TimePeriod` covers the build dates;
  - `Granularity="DAILY"`, `Metrics=["UnblendedCost"]`;
  - `GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}]`;
  - `Filter={"Not": {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Credit", "Refund"]}}}` for gross spend.

  It prints a markdown table of service → USD and the total. Note that Cost Explorer lags 8–24 h, so run it the day after the demo.
- [ ] **Step 3: Revise the cost write-up.**
  - Measured spend by service vs the spec's estimate.
  - $/GPU-hour actually used.
  - Measured calls per task at the target (from the dashboard).
  - Re-run `cost/fargate_cost.py` with ap-south-1 prices: add an `X86_AP_SOUTH_1 = Prices("x86 ap-south-1", 0.04256, 0.004655)` constant and use the measured calls per task.
  - The monthly A/B configs at ap-south-1 prices.

  Update the README cost section and `docs/writeup_pdf.py` if the user wants a PDF again; the PDF stays uncommitted.
- [ ] **Step 4: Commit, push and hand the user what remains:**
  - the reimbursement PDF from the Billing console (Bills → download the invoice);
  - the demo recording;
  - the teardown screenshot.
