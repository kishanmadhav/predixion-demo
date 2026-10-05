import httpx
import pytest

from voice_agent.providers.base import ErrorKind, ProviderError
from voice_agent.providers.http import parse_json, send


def client_returning(response: httpx.Response | Exception) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(response, Exception):
            raise response
        return response

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def send_and_classify(response: httpx.Response | Exception) -> ProviderError:
    async with client_returning(response) as client:
        with pytest.raises(ProviderError) as exc:
            await send(client, "llm", "POST", "http://provider/v1/x", json={})
    return exc.value


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (500, ErrorKind.SERVER_ERROR),
        (502, ErrorKind.SERVER_ERROR),
        (503, ErrorKind.SERVER_ERROR),
        (504, ErrorKind.SERVER_ERROR),
        (429, ErrorKind.RATE_LIMITED),
        (408, ErrorKind.TIMEOUT),
        (400, ErrorKind.CLIENT_ERROR),
        (404, ErrorKind.CLIENT_ERROR),
        (422, ErrorKind.CLIENT_ERROR),
        (401, ErrorKind.AUTH),
        (403, ErrorKind.AUTH),
    ],
)
async def test_status_codes_are_classified(status: int, kind: ErrorKind) -> None:
    err = await send_and_classify(httpx.Response(status, text="nope"))
    assert err.kind is kind
    assert err.status_code == status
    assert err.stage == "llm"


async def test_retry_after_seconds_header_is_parsed() -> None:
    err = await send_and_classify(httpx.Response(429, headers={"Retry-After": "1.5"}))
    assert err.retry_after == 1.5


async def test_unparseable_retry_after_is_ignored() -> None:
    err = await send_and_classify(httpx.Response(503, headers={"Retry-After": "soon"}))
    assert err.retry_after is None


async def test_timeouts_are_classified() -> None:
    err = await send_and_classify(httpx.ReadTimeout("slow"))
    assert err.kind is ErrorKind.TIMEOUT


async def test_connection_failures_are_classified() -> None:
    err = await send_and_classify(httpx.ConnectError("refused"))
    assert err.kind is ErrorKind.CONNECTION


async def test_success_returns_the_response() -> None:
    async with client_returning(httpx.Response(200, json={"ok": True})) as client:
        response = await send(client, "llm", "POST", "http://provider/v1/x", json={})
    assert parse_json("llm", response) == {"ok": True}


def test_invalid_json_is_malformed() -> None:
    with pytest.raises(ProviderError) as exc:
        parse_json("llm", httpx.Response(200, text="<html>oops"))
    assert exc.value.kind is ErrorKind.MALFORMED
