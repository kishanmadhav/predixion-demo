"""Build the three stage providers from configuration.

This is the single place that knows adapter names. Swapping the mock for an
open-weight model is a config change here, never a code change in the pipeline
or resilience layer.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from voice_agent.config import Settings
from voice_agent.providers.base import LlmProvider, SttProvider, TtsProvider
from voice_agent.providers.mock import MockLlm, MockStt, MockTts
from voice_agent.providers.openai_compat import OpenAICompatLlm, OpenAICompatStt, OpenAICompatTts


@dataclass(frozen=True)
class Providers:
    stt: SttProvider
    llm: LlmProvider
    tts: TtsProvider


def build_providers(settings: Settings, client: httpx.AsyncClient) -> Providers:
    stt: SttProvider
    llm: LlmProvider
    tts: TtsProvider

    if settings.stt_provider == "openai":
        stt = OpenAICompatStt(
            client,
            settings.stt_base_url,
            model=settings.stt_model,
            language=settings.stt_language,
            api_key=settings.stt_api_key,
        )
    else:
        stt = MockStt(client, settings.stt_base_url)

    if settings.llm_provider == "openai":
        llm = OpenAICompatLlm(
            client,
            settings.llm_base_url,
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            max_tokens=settings.llm_max_tokens,
        )
    else:
        llm = MockLlm(client, settings.llm_base_url)

    if settings.tts_provider == "openai":
        tts = OpenAICompatTts(
            client,
            settings.tts_base_url,
            model=settings.tts_model,
            voice=settings.tts_voice,
            api_key=settings.tts_api_key,
        )
    else:
        tts = MockTts(client, settings.tts_base_url)

    return Providers(stt=stt, llm=llm, tts=tts)
