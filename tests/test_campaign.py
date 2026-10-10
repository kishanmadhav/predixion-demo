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
        call_id = request.url.path.split("/")[3]
        headers = {"set-cookie": f"AWSALB={call_id}; Path=/"}
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
    end_by_call = {r.url.path.split("/")[3]: r for r in ends}
    for call_id, requests in by_call.items():
        assert "cookie" not in requests[0].headers
        assert requests[1].headers["cookie"] == f"AWSALB={call_id}"
        assert end_by_call[call_id].headers["cookie"] == f"AWSALB={call_id}"
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


async def test_malformed_200_is_counted_and_call_still_ends() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/end"):
            return httpx.Response(200, json={})
        return httpx.Response(200, content=b"<html>not json</html>")

    async with make_client("http://svc", transport=httpx.MockTransport(handler)) as client:
        campaign = Campaign(
            client,
            [Phase("p", 0.02, 300)],
            audio=[b"RIFF"],
            turns_per_call=2,
            gap_s=0.0,
            rng=random.Random(3),
            progress_every_s=999,
            out=lambda _: None,
        )
        stats = await campaign.run()
    s = stats["p"]
    ends = [r for r in seen if r.url.path.endswith("/end")]
    assert s.calls_started >= 1
    assert s.http_failures == 2 * s.calls_started
    assert s.errors["bad response"] == s.http_failures
    assert len(ends) == s.calls_started


async def test_draining_progress_line_shows_totals_across_phases() -> None:
    async with make_client("http://svc", transport=fake_service([])) as client:
        campaign = Campaign(
            client,
            [Phase("p", 0.02, 300)],
            audio=[b"RIFF"],
            turns_per_call=1,
            gap_s=0.0,
            rng=random.Random(4),
            progress_every_s=999,
            out=lambda _: None,
        )
        stats = await campaign.run()
    done = sum(stats["p"].turns.values())
    assert done >= 1
    line = campaign.progress_line(60.0)
    assert f"turns={done:<5}" in line
    assert "(all phases)" in line
    assert "phase=draining" in line
