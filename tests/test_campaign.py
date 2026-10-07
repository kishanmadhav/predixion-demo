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
