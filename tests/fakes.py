"""Scriptable in-memory providers and a fast runtime for pipeline/DLQ/API tests."""

from __future__ import annotations

import random
from pathlib import Path

from voice_agent.config import Settings
from voice_agent.providers.base import ChatMessage, ErrorKind, ProviderError
from voice_agent.providers.factory import Providers


def unavailable(stage: str) -> ProviderError:
    return ProviderError(stage, ErrorKind.SERVER_ERROR, "HTTP 503", status_code=503)


class FakeStage:
    """Fails while `failing` is True (or for the next `fail_next` calls), else succeeds."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        self.failing = False
        self.fail_next = 0
        self.calls = 0

    def check(self) -> None:
        self.calls += 1
        if self.failing:
            raise unavailable(self.stage)
        if self.fail_next > 0:
            self.fail_next -= 1
            raise unavailable(self.stage)


class FakeStt(FakeStage):
    def __init__(self) -> None:
        super().__init__("stt")

    async def transcribe(self, audio: bytes) -> str:
        self.check()
        return f"caller said {audio.decode(errors='replace')}"


class FakeLlm(FakeStage):
    def __init__(self) -> None:
        super().__init__("llm")
        self.seen: list[list[ChatMessage]] = []

    async def complete(self, messages: list[ChatMessage]) -> str:
        self.seen.append(messages)
        self.check()
        return f"reply to: {messages[-1].content}"


class FakeTts(FakeStage):
    def __init__(self) -> None:
        super().__init__("tts")

    async def synthesize(self, text: str) -> bytes:
        self.check()
        return f"AUDIO<{text}>".encode()


def fake_providers() -> tuple[Providers, FakeStt, FakeLlm, FakeTts]:
    stt, llm, tts = FakeStt(), FakeLlm(), FakeTts()
    return Providers(stt=stt, llm=llm, tts=tts), stt, llm, tts


def fast_settings(tmp_path: Path, **overrides: object) -> Settings:
    params: dict[str, object] = {
        "_env_file": None,
        "db_path": tmp_path / "va.db",
        "retry_base_delay_s": 0.0,
        "retry_max_delay_s": 0.0,
        "breaker_window": 10,
        "breaker_minimum_calls": 5,
        "breaker_open_s": 0.2,
        "breaker_half_open_calls": 2,
    }
    params.update(overrides)
    return Settings(**params)  # type: ignore[arg-type]


SEEDED = random.Random(0)
