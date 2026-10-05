"""Shared HTTP plumbing for adapters: turn httpx outcomes into `ProviderError`s.

This is the only place that decides what an HTTP failure *means*. Adapters call
`send()` and `parse_json()` and never see raw transport exceptions.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import SecretStr

from voice_agent.providers.base import ErrorKind, ProviderError

_BODY_SNIPPET = 200


def auth_headers(api_key: SecretStr | None) -> dict[str, str]:
    if api_key is None or not api_key.get_secret_value():
        return {}
    return {"Authorization": f"Bearer {api_key.get_secret_value()}"}


def join(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


async def send(
    client: httpx.AsyncClient, stage: str, method: str, url: str, **kwargs: Any
) -> httpx.Response:
    """Send a request; return the response only if it is a 2xx."""
    try:
        response = await client.request(method, url, **kwargs)
    except httpx.TimeoutException as exc:
        raise ProviderError(stage, ErrorKind.TIMEOUT, f"{type(exc).__name__}: {exc}") from exc
    except httpx.TransportError as exc:
        raise ProviderError(stage, ErrorKind.CONNECTION, f"{type(exc).__name__}: {exc}") from exc
    error = classify_status(stage, response)
    if error is not None:
        raise error
    return response


def classify_status(stage: str, response: httpx.Response) -> ProviderError | None:
    code = response.status_code
    if 200 <= code < 300:
        return None
    detail = f"HTTP {code}: {response.text[:_BODY_SNIPPET]}"
    if code == 429:
        kind = ErrorKind.RATE_LIMITED
    elif code == 408:
        kind = ErrorKind.TIMEOUT
    elif code in (401, 403):
        kind = ErrorKind.AUTH
    elif code >= 500:
        kind = ErrorKind.SERVER_ERROR
    else:
        kind = ErrorKind.CLIENT_ERROR
    return ProviderError(
        stage,
        kind,
        detail,
        status_code=code,
        retry_after=_retry_after(response.headers.get("Retry-After")),
    )


def _retry_after(value: str | None) -> float | None:
    # Only the delta-seconds form; HTTP-date values are rare from model servers.
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def parse_json(stage: str, response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise ProviderError(
            stage, ErrorKind.MALFORMED, f"invalid JSON body: {response.text[:_BODY_SNIPPET]!r}"
        ) from exc


def malformed(stage: str, what: str) -> ProviderError:
    return ProviderError(stage, ErrorKind.MALFORMED, what)
