"""Adapters for the local mock provider (`mock_provider`), a simple JSON API.

If the real mock you are handed speaks a different shape, this is the only file
that needs a sibling: the resilience layer and pipeline never change.
"""

from __future__ import annotations

import base64
import binascii

import httpx

from voice_agent.providers.base import ChatMessage
from voice_agent.providers.http import join, malformed, parse_json, send


def _text_field(stage: str, body: object, field: str) -> str:
    value = body.get(field) if isinstance(body, dict) else None
    if not isinstance(value, str):
        raise malformed(stage, f"expected string field {field!r} in response")
    return value


class MockStt:
    def __init__(self, client: httpx.AsyncClient, base_url: str) -> None:
        self._client = client
        self._url = join(base_url, "stt")

    async def transcribe(self, audio: bytes) -> str:
        response = await send(
            self._client,
            "stt",
            "POST",
            self._url,
            json={"audio_b64": base64.b64encode(audio).decode("ascii")},
        )
        return _text_field("stt", parse_json("stt", response), "text")


class MockLlm:
    def __init__(self, client: httpx.AsyncClient, base_url: str) -> None:
        self._client = client
        self._url = join(base_url, "llm")

    async def complete(self, messages: list[ChatMessage]) -> str:
        response = await send(
            self._client,
            "llm",
            "POST",
            self._url,
            json={"messages": [{"role": m.role, "content": m.content} for m in messages]},
        )
        return _text_field("llm", parse_json("llm", response), "text")


class MockTts:
    def __init__(self, client: httpx.AsyncClient, base_url: str) -> None:
        self._client = client
        self._url = join(base_url, "tts")

    async def synthesize(self, text: str) -> bytes:
        response = await send(self._client, "tts", "POST", self._url, json={"text": text})
        encoded = _text_field("tts", parse_json("tts", response), "audio_b64")
        try:
            return base64.b64decode(encoded, validate=True)
        except binascii.Error as exc:
            raise malformed("tts", "audio_b64 is not valid base64") from exc
