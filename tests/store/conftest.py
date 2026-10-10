from collections.abc import AsyncIterator
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from voice_agent.store import SqliteStore, Store
from voice_agent.store.dynamodb import TABLE_SCHEMA, DynamoStore

ATTEMPTS = [{"stage": "llm", "attempt": 1, "outcome": "error", "error_kind": "server_error"}]


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


async def degrade(store: Store, call_id: str = "c1", turn_id: str = "t1") -> int:
    await store.begin_turn(call_id, turn_id, {"audio_b64": "AAAA"})
    return await store.degrade_turn(
        call_id,
        turn_id,
        failed_stage="llm",
        error_kind="server_error",
        error_detail="[llm] server_error: 503",
        attempts=ATTEMPTS,
        partial={"transcript": "I can pay next week"},
    )
