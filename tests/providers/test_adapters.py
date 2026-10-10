import base64
import json
from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from voice_agent.config import Settings
from voice_agent.providers.base import ChatMessage, ErrorKind, ProviderError
from voice_agent.providers.factory import build_providers
from voice_agent.providers.mock import MockLlm, MockStt, MockTts
from voice_agent.providers.openai_compat import OpenAICompatLlm, OpenAICompatStt, OpenAICompatTts

Handler = Callable[[httpx.Request], httpx.Response]


class Recorder:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.response

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


# -- mock-provider adapters ---------------------------------------------------


async def test_mock_stt_posts_base64_audio_and_returns_text() -> None:
    rec = Recorder(httpx.Response(200, json={"text": "I can pay on Friday"}))
    async with rec.client() as client:
        text = await MockStt(client, "http://mock:9000/v1").transcribe(b"\x00\x01")
    assert text == "I can pay on Friday"
    assert rec.last.url == "http://mock:9000/v1/stt"
    assert json.loads(rec.last.content) == {"audio_b64": base64.b64encode(b"\x00\x01").decode()}


async def test_mock_llm_sends_chat_messages() -> None:
    rec = Recorder(httpx.Response(200, json={"text": "Thank you."}))
    async with rec.client() as client:
        reply = await MockLlm(client, "http://mock:9000/v1").complete(
            [ChatMessage("system", "be polite"), ChatMessage("user", "hi")]
        )
    assert reply == "Thank you."
    assert rec.last.url == "http://mock:9000/v1/llm"
    assert json.loads(rec.last.content)["messages"][1] == {"role": "user", "content": "hi"}


async def test_mock_tts_decodes_audio() -> None:
    audio = base64.b64encode(b"RIFFdata").decode()
    rec = Recorder(httpx.Response(200, json={"audio_b64": audio}))
    async with rec.client() as client:
        assert await MockTts(client, "http://mock:9000/v1").synthesize("hi") == b"RIFFdata"


@pytest.mark.parametrize(
    "body", [{"text": 42}, {"wrong": "shape"}, {"audio_b64": "!!!not base64!!!"}]
)
async def test_mock_adapters_reject_malformed_bodies(body: dict[str, object]) -> None:
    rec = Recorder(httpx.Response(200, json=body))
    async with rec.client() as client:
        adapter = (
            MockTts(client, "http://m/v1")
            if "audio_b64" in body
            else MockStt(client, "http://m/v1")
        )
        with pytest.raises(ProviderError) as exc:
            if isinstance(adapter, MockTts):
                await adapter.synthesize("x")
            else:
                await adapter.transcribe(b"x")
    assert exc.value.kind is ErrorKind.MALFORMED


# -- OpenAI-compatible (open-weight server) adapters ----------------------------


async def test_openai_stt_sends_multipart_transcription_request() -> None:
    rec = Recorder(httpx.Response(200, json={"text": "hello"}))
    async with rec.client() as client:
        stt = OpenAICompatStt(
            client, "http://speaches:8000/v1", model="Systran/faster-whisper-small", language="en"
        )
        assert await stt.transcribe(b"WAVBYTES") == "hello"
    req = rec.last
    assert req.url == "http://speaches:8000/v1/audio/transcriptions"
    assert req.headers["content-type"].startswith("multipart/form-data")
    body = req.content
    assert b'name="file"; filename="audio.wav"' in body
    assert b"WAVBYTES" in body
    assert b'name="model"\r\n\r\nSystran/faster-whisper-small' in body
    assert b'name="language"\r\n\r\nen' in body
    assert "authorization" not in req.headers


async def test_openai_llm_sends_chat_completion_and_reads_first_choice() -> None:
    rec = Recorder(
        httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "Sure."}}]}
        )
    )
    async with rec.client() as client:
        llm = OpenAICompatLlm(
            client, "http://ollama:11434/v1", model="qwen2.5:1.5b", api_key=SecretStr("sk-local")
        )
        assert await llm.complete([ChatMessage("user", "hi")]) == "Sure."
    req = rec.last
    assert req.url == "http://ollama:11434/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-local"
    body = json.loads(req.content)
    assert body["model"] == "qwen2.5:1.5b"
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["stream"] is False


async def test_openai_llm_rejects_responses_without_content() -> None:
    rec = Recorder(httpx.Response(200, json={"choices": []}))
    async with rec.client() as client:
        with pytest.raises(ProviderError) as exc:
            await OpenAICompatLlm(client, "http://x/v1", model="m").complete(
                [ChatMessage("user", "hi")]
            )
    assert exc.value.kind is ErrorKind.MALFORMED


async def test_openai_tts_requests_speech_and_returns_raw_audio() -> None:
    rec = Recorder(httpx.Response(200, content=b"RIFF....WAVE"))
    async with rec.client() as client:
        tts = OpenAICompatTts(client, "http://kokoro:8880/v1", model="kokoro", voice="af_bella")
        assert await tts.synthesize("Hello") == b"RIFF....WAVE"
    req = rec.last
    assert req.url == "http://kokoro:8880/v1/audio/speech"
    assert json.loads(req.content) == {
        "model": "kokoro",
        "input": "Hello",
        "voice": "af_bella",
        "response_format": "wav",
    }


async def test_openai_tts_rejects_empty_audio() -> None:
    rec = Recorder(httpx.Response(200, content=b""))
    async with rec.client() as client:
        with pytest.raises(ProviderError) as exc:
            await OpenAICompatTts(client, "http://x/v1", model="m", voice="v").synthesize("hi")
    assert exc.value.kind is ErrorKind.MALFORMED


async def test_trailing_slash_in_base_url_is_tolerated() -> None:
    rec = Recorder(httpx.Response(200, json={"text": "ok"}))
    async with rec.client() as client:
        await MockStt(client, "http://mock:9000/v1/").transcribe(b"x")
    assert rec.last.url == "http://mock:9000/v1/stt"


# -- factory --------------------------------------------------------------------


async def test_factory_builds_mock_adapters_by_default() -> None:
    async with httpx.AsyncClient() as client:
        providers = build_providers(Settings(_env_file=None), client)
    assert isinstance(providers.stt, MockStt)
    assert isinstance(providers.llm, MockLlm)
    assert isinstance(providers.tts, MockTts)


async def test_factory_mixes_adapters_per_stage_from_config() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="openai",
        llm_base_url="http://ollama:11434/v1",
        llm_model="llama3.2:3b",
        tts_provider="openai",
    )
    async with httpx.AsyncClient() as client:
        providers = build_providers(settings, client)
    assert isinstance(providers.stt, MockStt)
    assert isinstance(providers.llm, OpenAICompatLlm)
    assert isinstance(providers.tts, OpenAICompatTts)


def test_settings_read_stage_config_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_API_KEY", "from-env")
    settings = Settings(_env_file=None)
    assert settings.llm_provider == "openai"
    assert settings.llm_api_key is not None
    assert settings.llm_api_key.get_secret_value() == "from-env"
    assert "from-env" not in repr(settings)


async def test_factory_caps_llm_reply_length_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_MAX_TOKENS", raising=False)
    assert Settings(_env_file=None).llm_max_tokens == 200
    monkeypatch.setenv("LLM_MAX_TOKENS", "80")
    settings = Settings(_env_file=None, llm_provider="openai", llm_base_url="http://vllm/v1")
    assert settings.llm_max_tokens == 80
    rec = Recorder(httpx.Response(200, json={"choices": [{"message": {"content": "Sure."}}]}))
    async with rec.client() as client:
        providers = build_providers(settings, client)
        await providers.llm.complete([ChatMessage("user", "hi")])
    assert json.loads(rec.last.content)["max_tokens"] == 80


def test_llm_max_tokens_must_be_positive() -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, llm_max_tokens=0)
