"""Speech provider interfaces.

All audio crossing these interfaces is telephony audio: 8 kHz, mono, 8-bit G.711 mu-law, the
format Twilio Media Streams sends and expects. Providers convert internally if their API needs
something else.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import ClassVar

TELEPHONY_SAMPLE_RATE = 8000
# Twilio sends 20 ms frames: 160 mu-law bytes at 8 kHz.
TELEPHONY_FRAME_BYTES = 160


class SpeechError(Exception):
    """A speech provider failed (network, auth, quota, unsupported config)."""


class MissingCredentialsError(SpeechError):
    """The provider cannot be built: env variables or SDK packages are missing."""

    def __init__(self, provider: str, missing: list[str]) -> None:
        self.provider = provider
        self.missing = missing
        super().__init__(f"{provider}: missing {', '.join(missing)}")


@dataclass(frozen=True)
class STTConfig:
    # BCP-47 codes; the first is the primary language, the rest are hints for auto-detection.
    languages: tuple[str, ...] = ("lt-LT",)
    interim_results: bool = True


@dataclass(frozen=True)
class TranscriptEvent:
    text: str
    is_final: bool
    language: str | None = None
    confidence: float | None = None


@dataclass(frozen=True)
class VoiceConfig:
    language: str = "lt-LT"
    # Provider-specific voice name / id. None means the provider's configured default.
    voice: str | None = None
    speaking_rate: float = 1.0


class STTProvider(ABC):
    name: ClassVar[str]

    @abstractmethod
    def stream(
        self, audio: AsyncIterator[bytes], config: STTConfig
    ) -> AsyncIterator[TranscriptEvent]:
        """Transcribe a stream of 8 kHz mu-law chunks.

        Yields interim events (is_final=False, each one replaces the previous interim text) and
        final events (one per finished segment). Ends after the audio iterator is exhausted and
        the provider has flushed its last result. Raises SpeechError on failure.
        """

    async def aclose(self) -> None:  # noqa: B027 - optional hook
        """Release clients/connections."""


class TTSProvider(ABC):
    name: ClassVar[str]

    @abstractmethod
    def synthesize(self, text: str, voice: VoiceConfig) -> AsyncIterator[bytes]:
        """Synthesize text, yielding 8 kHz mu-law chunks as soon as they arrive.

        Raises SpeechError on failure.
        """

    async def aclose(self) -> None:  # noqa: B027 - optional hook
        """Release clients/connections."""
