from collections.abc import AsyncIterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

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


async def test_list_dead_letters_pages_past_one_query_page(
    dynamo: tuple[Any, DynamoStore],
) -> None:
    _, store = dynamo
    for n in range(120):
        await store.begin_turn("c1", f"t{n}", {"audio_b64": "AAAA"})
        await _degrade(store, f"t{n}")
    assert len(await store.list_dead_letters(limit=1000)) == 120
    assert (await store.dead_letter_counts())["pending"] == 120
