from collections import Counter
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from tests.fakes import fake_providers, fast_settings
from voice_agent.api import create_app
from voice_agent.loadgen import LoadReport, dlq_counts, format_load_report
from voice_agent.wiring import open_runtime


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    providers, _, llm, _ = fake_providers()
    llm.failing = True
    runtime = await open_runtime(fast_settings(tmp_path), providers=providers)
    transport = httpx.ASGITransport(app=create_app(runtime=runtime))
    async with httpx.AsyncClient(transport=transport, base_url="http://va") as http:
        yield http
    await runtime.close()


async def test_dlq_counts_come_from_the_dlq_endpoint(client: httpx.AsyncClient) -> None:
    for i in range(2):
        await client.post("/v1/calls/c1/turns", json={"turn_id": f"t{i}", "audio_b64": "aGk="})
    assert await dlq_counts(client) == {"pending": 2, "replaying": 0, "resolved": 0}


def test_load_report_shows_breakers_and_dead_letter_counts() -> None:
    report = LoadReport(statuses=Counter(completed=3, degraded=1))
    health = {"status": "ok", "breakers": {"llm": {"state": "open"}}}
    text = format_load_report(
        report, health, None, dead_letters={"pending": 1, "replaying": 0, "resolved": 0}
    )
    assert "llm=open" in text
    assert "pending=1, replaying=0, resolved=0" in text
