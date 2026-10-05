"""The model-agnostic dependency boundary.

Everything the pipeline and resilience layer know about STT/LLM/TTS lives here:
three single-method Protocols and one error type. Adapters (mock, OpenAI-compatible
open-weight servers, anything else) implement the Protocols and translate their
transport failures into `ProviderError`. Nothing outside `providers/` knows which
adapter is in use.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable


class ErrorKind(StrEnum):
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    MALFORMED = "malformed"
    CLIENT_ERROR = "client_error"
    AUTH = "auth"
    CIRCUIT_OPEN = "circuit_open"
    DEADLINE = "deadline"


# Failures that say "the provider is unhealthy right now": worth another attempt,
# and they count toward opening the circuit.
_TRANSIENT = frozenset(
    {
        ErrorKind.TIMEOUT,
        ErrorKind.CONNECTION,
        ErrorKind.RATE_LIMITED,
        ErrorKind.SERVER_ERROR,
        ErrorKind.MALFORMED,
    }
)


class ProviderError(Exception):
    """A failed call to a pipeline dependency, classified for the resilience layer."""

    def __init__(
        self,
        stage: str,
        kind: ErrorKind,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(f"[{stage}] {kind.value}: {message}")
        self.stage = stage
        self.kind = kind
        self.message = message
        self.status_code = status_code
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.kind in _TRANSIENT

    @property
    def counts_as_failure(self) -> bool:
        # A 4xx means *our* request or config is wrong; the provider is healthy,
        # so it must not push the breaker toward open.
        return self.kind in _TRANSIENT


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str


@runtime_checkable
class SttProvider(Protocol):
    async def transcribe(self, audio: bytes) -> str: ...


@runtime_checkable
class LlmProvider(Protocol):
    async def complete(self, messages: list[ChatMessage]) -> str: ...


@runtime_checkable
class TtsProvider(Protocol):
    async def synthesize(self, text: str) -> bytes: ...
