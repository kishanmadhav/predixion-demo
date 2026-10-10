import asyncio
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from voice_agent.store import DEAD_LETTER_STATUSES
from voice_agent.store.dynamodb import TABLE_SCHEMA, DynamoStore


@pytest.fixture
async def dynamo(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[Any, DynamoStore]]:
    for var in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    with mock_aws():
        client = boto3.client("dynamodb", region_name="ap-south-1")
        client.create_table(TableName="test-state", **TABLE_SCHEMA)
        store = DynamoStore(client, "test-state")
        yield client, store
        await store.close()


async def _degrade(store: DynamoStore, turn_id: str) -> int:
    return await store.degrade_turn(
        "c1",
        turn_id,
        failed_stage="llm",
        error_kind="server_error",
        error_detail="x",
        attempts=[],
        partial={},
    )


async def test_dead_letter_ids_are_unique_and_increasing(
    dynamo: tuple[Any, DynamoStore],
) -> None:
    _, store = dynamo
    ids = []
    for n in range(3):
        await store.begin_turn("c1", f"t{n}", {"audio_b64": "AAAA"})
        ids.append(await _degrade(store, f"t{n}"))
    assert ids == sorted(ids) and len(set(ids)) == 3


async def test_a_turn_is_never_dead_lettered_twice(dynamo: tuple[Any, DynamoStore]) -> None:
    _, store = dynamo
    await store.begin_turn("c1", "t1", {"audio_b64": "AAAA"})
    await _degrade(store, "t1")
    with pytest.raises(LookupError):
        await _degrade(store, "t1")
    assert (await store.dead_letter_counts())["pending"] == 1


async def test_finished_turns_leave_the_status_index(dynamo: tuple[Any, DynamoStore]) -> None:
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


async def test_recover_does_not_reset_a_claim_renewed_after_the_index_read(
    dynamo: tuple[Any, DynamoStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, store = dynamo
    await store.begin_turn("c1", "t1", {"audio_b64": "AAAA"})
    dlq_id = await _degrade(store, "t1")
    assert await store.claim_dead_letter(dlq_id) is not None
    stale_snapshot = await store._index("DLQ#replaying")
    await store.release_dead_letter(dlq_id, error="x", attempts=[])
    await asyncio.sleep(0.01)
    assert await store.claim_dead_letter(dlq_id) is not None  # renewed: new claimed_at

    original = store._index

    async def stale_index(pk: str, **kwargs: Any) -> list[dict[str, Any]]:
        return stale_snapshot if pk == "DLQ#replaying" else await original(pk, **kwargs)

    monkeypatch.setattr(store, "_index", stale_index)
    await store.recover(stale_after_s=-1)  # cutoff in the future: the snapshot looks stale
    entry = await store.get_dead_letter(dlq_id)
    assert entry is not None and entry.status == "replaying"


async def test_recover_skips_a_turn_another_task_is_dead_lettering(
    dynamo: tuple[Any, DynamoStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, store = dynamo
    for turn_id in ("t1", "t2"):
        await store.begin_turn("c1", turn_id, {"audio_b64": "AAAA"})
    original = store._call

    async def conflicting(operation: str, **kwargs: Any) -> dict[str, Any]:
        if operation == "transact_write_items" and any(
            "TURN#t1" in str(item) for item in kwargs["TransactItems"]
        ):
            raise ClientError(
                {
                    "Error": {"Code": "TransactionCanceledException", "Message": "cancelled"},
                    "CancellationReasons": [{"Code": "TransactionConflict"}, {"Code": "None"}],
                },
                "TransactWriteItems",
            )
        return await original(operation, **kwargs)

    monkeypatch.setattr(store, "_call", conflicting)
    ids = await store.recover(stale_after_s=-1)
    assert len(ids) == 1
    t1, t2 = await store.get_turn("c1", "t1"), await store.get_turn("c1", "t2")
    assert t1 is not None and t1.status.value == "in_progress"
    assert t2 is not None and t2.status.value == "interrupted"


async def test_list_dead_letters_pages_past_one_query_page(
    dynamo: tuple[Any, DynamoStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, store = dynamo
    store.page_size = 25
    for n in range(120):
        await store.begin_turn("c1", f"t{n}", {"audio_b64": "AAAA"})
        await _degrade(store, f"t{n}")
    queries = 0
    original = store._call

    async def counting(operation: str, **kwargs: Any) -> dict[str, Any]:
        nonlocal queries
        queries += operation == "query"
        return await original(operation, **kwargs)

    monkeypatch.setattr(store, "_call", counting)
    assert len(await store.list_dead_letters(limit=1000)) == 120
    listing_queries = queries
    assert listing_queries > len(DEAD_LETTER_STATUSES)  # more than one page for "pending"
    assert (await store.dead_letter_counts())["pending"] == 120
    assert queries - listing_queries > len(DEAD_LETTER_STATUSES)


async def test_resolve_does_not_create_a_missing_turn(dynamo: tuple[Any, DynamoStore]) -> None:
    client, store = dynamo
    await store.begin_turn("c1", "t1", {"audio_b64": "AAAA"})
    dlq_id = await _degrade(store, "t1")
    client.delete_item(
        TableName="test-state",
        Key={"pk": {"S": "CALL#c1"}, "sk": {"S": "TURN#t1"}},
    )
    assert await store.claim_dead_letter(dlq_id) is not None
    assert await store.resolve_dead_letter(dlq_id, {"transcript": "a"}, []) is False
    assert await store.get_turn("c1", "t1") is None


_LARGE = ("payload", "request", "attempts", "partial", "result")


def test_status_index_projects_no_audio_or_large_attributes() -> None:
    (index,) = TABLE_SCHEMA["GlobalSecondaryIndexes"]
    projection = index["Projection"]
    assert projection["ProjectionType"] == "INCLUDE"
    assert not set(_LARGE) & set(projection["NonKeyAttributes"])
    assert {"id", "call_id", "turn_id", "status", "claimed_at"} <= set(
        projection["NonKeyAttributes"]
    )


def test_terraform_index_projection_matches_the_table_schema() -> None:
    tf = (Path(__file__).parents[2] / "infra" / "dynamodb.tf").read_text()
    assert re.search(r'projection_type\s*=\s*"INCLUDE"', tf)
    match = re.search(r"non_key_attributes\s*=\s*\[([^\]]*)\]", tf)
    assert match is not None
    tf_attributes = re.findall(r'"([^"]+)"', match.group(1))
    (index,) = TABLE_SCHEMA["GlobalSecondaryIndexes"]
    assert sorted(tf_attributes) == sorted(index["Projection"]["NonKeyAttributes"])


async def test_index_reads_never_carry_the_audio(dynamo: tuple[Any, DynamoStore]) -> None:
    client, store = dynamo
    await store.begin_turn("c1", "live", {"audio_b64": "AAAA"})
    await store.begin_turn("c1", "t1", {"audio_b64": "AAAA"})
    await _degrade(store, "t1")
    for pk in ("TURN#in_progress", "DLQ#pending"):
        page = client.query(
            TableName="test-state",
            IndexName="by_status",
            KeyConditionExpression="gsi1pk = :pk",
            ExpressionAttributeValues={":pk": {"S": pk}},
        )
        assert len(page["Items"]) == 1
        assert not set(_LARGE) & set(page["Items"][0])


async def test_listing_dead_letters_reads_only_the_index(
    dynamo: tuple[Any, DynamoStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, store = dynamo
    for n in range(3):
        await store.begin_turn("c1", f"t{n}", {"audio_b64": "AAAA"})
        await _degrade(store, f"t{n}")
    operations: list[str] = []
    original = store._call

    async def recording(operation: str, **kwargs: Any) -> dict[str, Any]:
        operations.append(operation)
        return await original(operation, **kwargs)

    monkeypatch.setattr(store, "_call", recording)
    listed = await store.list_dead_letters()
    assert len(listed) == 3 and set(operations) == {"query"}
    assert all(d.payload == {} and d.attempts == [] and d.partial == {} for d in listed)


async def test_turn_lookups_by_call_never_read_the_audio(
    dynamo: tuple[Any, DynamoStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, store = dynamo
    await store.begin_turn("c1", "t1", {"audio_b64": "AAAA"})
    await store.complete_turn("c1", "t1", {"transcript": "a", "reply_text": "A"}, [])
    await store.begin_turn("c1", "t2", {"audio_b64": "AAAA"})
    await _degrade(store, "t2")
    await store.begin_turn("c1", "t3", {"audio_b64": "AAAA"})
    returned: list[dict[str, Any]] = []
    original = store._call

    async def recording(operation: str, **kwargs: Any) -> dict[str, Any]:
        page = await original(operation, **kwargs)
        if operation == "query":
            returned.extend(page.get("Items", []))
        return page

    monkeypatch.setattr(store, "_call", recording)
    assert await store.history_before("c1", "t3", limit=5) == [("a", "A")]
    assert await store.consecutive_degraded("c1") == 1
    turns = await store.list_turns("c1")
    assert [t.turn_id for t in turns] == ["t1", "t2", "t3"]
    assert turns[0].result == {"transcript": "a", "reply_text": "A"}
    assert turns[1].failed_stage == "llm" and turns[1].status.value == "degraded"
    assert all(t.request == {} for t in turns)
    assert returned and not any("request" in item for item in returned)
