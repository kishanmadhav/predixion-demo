"""Service configuration, read from environment variables (and an optional `.env`).

No secret has a default. API keys are `SecretStr`, so they never appear in logs,
reprs or error messages.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ProviderKind = Literal["mock", "openai"]

DEFAULT_SYSTEM_PROMPT = (
    "You are a courteous collections assistant calling on behalf of a lender. "
    "Be brief (one or two sentences), never threaten, never discuss the debt with anyone "
    "other than the account holder, and offer to connect the caller to a human agent "
    "whenever they ask."
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Service
    host: str = "127.0.0.1"
    port: int = 8080
    db_path: Path = Path("data/voice_agent.db")
    log_level: str = "INFO"

    # Turn budget: a caller will not wait forever for the agent to speak.
    turn_deadline_s: float = Field(6.0, gt=0)
    handoff_after_degraded: int = Field(2, ge=1)
    llm_history_turns: int = Field(6, ge=0)
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    # Optional directory with pre-recorded retry_prompt.wav / handoff.wav.
    fallback_audio_dir: Path | None = None

    # Retry
    retry_max_attempts: int = Field(3, ge=1)
    retry_base_delay_s: float = Field(0.1, ge=0)
    retry_max_delay_s: float = Field(2.0, ge=0)

    # Circuit breaker (one per stage)
    breaker_window: int = Field(50, ge=1)
    breaker_minimum_calls: int = Field(20, ge=1)
    breaker_failure_threshold: float = Field(0.5, gt=0, le=1)
    breaker_open_s: float = Field(5.0, ge=0)
    breaker_half_open_calls: int = Field(5, ge=1)

    # STT
    stt_provider: ProviderKind = "mock"
    stt_base_url: str = "http://127.0.0.1:9000/v1"
    stt_model: str = "Systran/faster-whisper-small"
    stt_language: str | None = "en"
    stt_api_key: SecretStr | None = None
    stt_timeout_s: float = Field(1.5, gt=0)

    # LLM
    llm_provider: ProviderKind = "mock"
    llm_base_url: str = "http://127.0.0.1:9000/v1"
    llm_model: str = "qwen2.5:1.5b"
    llm_api_key: SecretStr | None = None
    llm_timeout_s: float = Field(3.0, gt=0)

    # TTS
    tts_provider: ProviderKind = "mock"
    tts_base_url: str = "http://127.0.0.1:9000/v1"
    tts_model: str = "kokoro"
    tts_voice: str = "af_bella"
    tts_api_key: SecretStr | None = None
    tts_timeout_s: float = Field(1.5, gt=0)
