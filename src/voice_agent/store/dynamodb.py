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
  for the lease sweeper; items leave it when they no longer need finding. It projects
  only small bookkeeping attributes, never the request audio or results, so the cost
  of counting and listing dead letters does not grow with the audio they hold.
  Listings built from it are therefore summary records (see `list_dead_letters`).

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

# Non-key attributes the status index carries. Keep in sync with infra/dynamodb.tf (a test
# checks). Never add payload, request, attempts, partial or result: they hold the audio.
INDEX_ATTRIBUTES = (
    "id",
    "call_id",
    "turn_id",
    "status",
    "reason",
    "failed_stage",
    "error_kind",
    "replay_count",
    "created_at",
    "claimed_at",
    "updated_at",
)

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
            "Projection": {
                "ProjectionType": "INCLUDE",
                "NonKeyAttributes": list(INDEX_ATTRIBUTES),
            },
        }
    ],
    "BillingMode": "PAY_PER_REQUEST",
}

# What `_call_turns` reads: a Turn without its request audio (aliased: several of these
# are DynamoDB reserved words).
_CALL_TURN_ATTRIBUTES = (
    "call_id",
    "turn_id",
    "arrival",
    "status",
    "action",
    "result",
    "failed_stage",
    "error_kind",
    "attempts",
    "created_at",
    "updated_at",
)

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


def _transaction_lost(exc: ClientError) -> bool:
    """A cancelled transaction another task won: its condition failed or it collided
    with a concurrent transaction on the same item."""
    if exc.response.get("Error", {}).get("Code", "") != "TransactionCanceledException":
        return False
    reasons = exc.response.get("CancellationReasons") or []
    if any(r.get("Code") in ("ConditionalCheckFailed", "TransactionConflict") for r in reasons):
        return True
    text = str(exc)
    return "ConditionalCheckFailed" in text or "TransactionConflict" in text


def _turn(d: dict[str, Any]) -> Turn:
    return Turn(
        call_id=d["call_id"],
        turn_id=d["turn_id"],
        status=TurnStatus(d["status"]),
        action=d.get("action"),
        request=loads(d.get("request")) or {},  # left out of call listings
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


def _listed_dead_letter(d: dict[str, Any]) -> DeadLetter:
    """A dead letter as the status index holds it: every `summary()` field is real; the
    payload, attempts, partial result and replay history are not projected and come back
    empty. Use `get_dead_letter` (or `claim_dead_letter`) for the full entry."""
    return DeadLetter(
        id=d["id"],
        call_id=d["call_id"],
        turn_id=d["turn_id"],
        reason=d["reason"],
        failed_stage=d.get("failed_stage"),
        error_kind=d.get("error_kind"),
        error_detail=None,
        payload={},
        attempts=[],
        partial={},
        status=d["status"],
        replay_count=d.get("replay_count", 0),
        last_replay_error=None,
        replay_attempts=None,
        created_at=d["created_at"],
        claimed_at=d.get("claimed_at"),
        last_replay_at=None,
        resolved_at=None,
    )


class DynamoStore:
    shared = True

    def __init__(self, client: Any, table: str) -> None:
        self._client = client
        self._table = table
        self.page_size: int | None = None  # Limit per query page; tests use it to force paging

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
            if self.page_size:
                page_args["Limit"] = self.page_size
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
        """Every turn of a call, in arrival order, without the request audio: history,
        degraded counts and the call listing never need it."""
        items = await self._query(
            KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={":pk": {"S": f"CALL#{call_id}"}, ":prefix": {"S": "TURN#"}},
            ProjectionExpression=", ".join(f"#{a}" for a in _CALL_TURN_ATTRIBUTES),
            ExpressionAttributeNames={f"#{a}": a for a in _CALL_TURN_ATTRIBUTES},
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
        """Turns without their request audio (`request` is `{}`); use `get_turn` for it."""
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
        skip_lost: bool = False,
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
            if _condition_failed(exc) or (skip_lost and _transaction_lost(exc)):
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
                    await self._reset_stale_claim(entry["id"], entry.get("claimed_at"))
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
                            skip_lost=True,
                        )
                    )
                except LookupError:
                    continue
            return ids

        return await self._write(work)

    # -- dead letters ---------------------------------------------------------

    async def _reset_stale_claim(self, dlq_id: int, claimed_at: str | None) -> bool:
        """Release a replay claim, but only the exact claim that was seen as stale: a
        claim another task renewed since the (eventually consistent) index read stays."""
        args = _update(
            {"status": "pending", "gsi1pk": "DLQ#pending"}, condition={"status": "replaying"}
        )
        if claimed_at is None:
            args["ConditionExpression"] += " AND attribute_not_exists(claimed_at)"
        else:
            args["ConditionExpression"] += " AND claimed_at = :seen_claim"
            args["ExpressionAttributeValues"][":seen_claim"] = {"S": claimed_at}
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
        """Summary records straight from the status index (see `_listed_dead_letter`):
        listing 1,000 entries must not read 1,000 payloads of audio."""
        statuses = [status] if status is not None else list(DEAD_LETTER_STATUSES)
        entries: list[dict[str, Any]] = []
        for s in statuses:
            found = await self._index(f"DLQ#{s}", limit=None if call_id else limit)
            entries.extend(e for e in found if call_id is None or e["call_id"] == call_id)
        entries.sort(key=lambda e: int(e["id"]))
        return [_listed_dead_letter(e) for e in entries[:limit]]

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
                if self.page_size:
                    args["Limit"] = self.page_size
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
            complete["ConditionExpression"] = "attribute_exists(pk)"
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
