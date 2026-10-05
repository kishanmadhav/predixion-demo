"""Adapters for open-weight model servers that expose the OpenAI-compatible API.

* STT  `POST {base}/audio/transcriptions` (multipart) - Speaches (faster-whisper),
       or any whisper server implementing the OpenAI transcription API.
* LLM  `POST {base}/chat/completions` - vLLM, Ollama, llama.cpp `llama-server`, TGI, SGLang.
* TTS  `POST {base}/audio/speech` - Kokoro-FastAPI, Speaches (Kokoro/Piper).

`base_url` follows the OpenAI SDK convention and includes the `/v1` suffix, e.g.
`http://localhost:11434/v1` for Ollama. The API key is optional (most local servers
need none) and is sent as a Bearer token only when configured.
"""

from __future__ import annotations

import httpx
from pydantic import SecretStr

from voice_agent.providers.base import ChatMessage
from voice_agent.providers.http import auth_headers, join, malformed, parse_json, send


class OpenAICompatStt:
    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        *,
        model: str,
        language: str | None = None,
        api_key: SecretStr | None = None,
    ) -> None:
        self._client = client
        self._url = join(base_url, "audio/transcriptions")
        self._model = model
        self._language = language
        self._headers = auth_headers(api_key)

    async def transcribe(self, audio: bytes) -> str:
        data = {"model": self._model, "response_format": "json"}
        if self._language:
            data["language"] = self._language
        response = await send(
            self._client,
            "stt",
            "POST",
            self._url,
            files={"file": ("audio.wav", audio, "audio/wav")},
            data=data,
            headers=self._headers,
        )
        body = parse_json("stt", response)
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str):
            raise malformed("stt", "transcription response has no 'text'")
        return text.strip()


class OpenAICompatLlm:
    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        *,
        model: str,
        api_key: SecretStr | None = None,
        temperature: float = 0.3,
        max_tokens: int = 200,
    ) -> None:
        self._client = client
        self._url = join(base_url, "chat/completions")
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._headers = auth_headers(api_key)

    async def complete(self, messages: list[ChatMessage]) -> str:
        response = await send(
            self._client,
            "llm",
            "POST",
            self._url,
            json={
                "model": self._model,
                "messages": [{"role": m.role, "content": m.content} for m in messages],
                "temperature": self._temperature,
                "max_tokens": self._max_tokens,
                "stream": False,
            },
            headers=self._headers,
        )
        body = parse_json("llm", response)
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise malformed("llm", "chat completion has no choices[0].message.content") from exc
        if not isinstance(content, str) or not content.strip():
            raise malformed("llm", "chat completion content is empty")
        return content.strip()


class OpenAICompatTts:
    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        *,
        model: str,
        voice: str,
        api_key: SecretStr | None = None,
        response_format: str = "wav",
    ) -> None:
        self._client = client
        self._url = join(base_url, "audio/speech")
        self._model = model
        self._voice = voice
        self._format = response_format
        self._headers = auth_headers(api_key)

    async def synthesize(self, text: str) -> bytes:
        response = await send(
            self._client,
            "tts",
            "POST",
            self._url,
            json={
                "model": self._model,
                "input": text,
                "voice": self._voice,
                "response_format": self._format,
            },
            headers=self._headers,
        )
        if not response.content:
            raise malformed("tts", "speech response has an empty body")
        return response.content
