from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

from services.elevenlabs import ElevenLabsTTSConfig, synthesize_speech_bytes


class SpeechSynth(ABC):
    """The seam between a video job and text-to-speech.

    Deliberately thin: one call, bytes back. Caching lives above this seam, in
    the job that owns the output directory — a cache hit is reported to the user
    as a job status, which makes it job policy rather than synthesis policy.
    """

    @abstractmethod
    async def synthesize(self, text: str, *, config: ElevenLabsTTSConfig) -> bytes:
        """Render ``text`` to audio bytes. Raises if the provider returns none."""


class ElevenLabsSpeech(SpeechSynth):
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def synthesize(self, text: str, *, config: ElevenLabsTTSConfig) -> bytes:
        return await asyncio.to_thread(
            synthesize_speech_bytes, text, api_key=self._api_key, config=config
        )
