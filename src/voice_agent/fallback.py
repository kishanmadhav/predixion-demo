"""Pre-rendered fallback prompts played when a turn cannot be completed.

The fallback must not depend on the pipeline that just failed, so its audio is
loaded once at startup rather than synthesized per turn. In production these are
pre-recorded, compliance-approved prompts; drop `retry_prompt.wav` / `handoff.wav`
into `FALLBACK_AUDIO_DIR` to use them. Without them, a short placeholder tone is
generated so the service runs out of the box.
"""

from __future__ import annotations

import io
import math
import struct
import wave
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class Action(StrEnum):
    """What the telephony layer should do after playing the agent's audio."""

    CONTINUE = "continue"  # normal turn, keep listening
    RETRY_PROMPT = "retry_prompt"  # we failed; ask the caller to repeat
    HANDOFF = "handoff"  # repeated failure; transfer to a human or schedule a callback


FALLBACK_TEXT = {
    Action.RETRY_PROMPT: "Sorry, I'm having trouble on my end. Could you say that again?",
    Action.HANDOFF: (
        "I'm sorry, I'm having technical difficulties. I'm transferring you to a member of "
        "our team now. If no one is available, we will call you back today."
    ),
}


@dataclass(frozen=True)
class FallbackPrompt:
    text: str
    audio: bytes


def _placeholder_tone(seconds: float = 0.4, hz: float = 440.0, rate: int = 8000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        frames = int(seconds * rate)
        samples = (int(4000 * math.sin(2 * math.pi * hz * i / rate)) for i in range(frames))
        wav.writeframes(b"".join(struct.pack("<h", s) for s in samples))
    return buffer.getvalue()


def load_fallbacks(audio_dir: Path | None = None) -> dict[Action, FallbackPrompt]:
    prompts = {}
    for action, text in FALLBACK_TEXT.items():
        path = audio_dir / f"{action.value}.wav" if audio_dir else None
        audio = path.read_bytes() if path and path.is_file() else _placeholder_tone()
        prompts[action] = FallbackPrompt(text=text, audio=audio)
    return prompts
