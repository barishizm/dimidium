"""Build STT/TTS providers by name (STT_PROVIDER / TTS_PROVIDER)."""

from collections.abc import Callable

from app.config import Settings
from app.speech import azure_provider, deepgram_provider, elevenlabs_provider, google_provider
from app.speech.base import MissingCredentialsError, STTProvider, TTSProvider

STT_PROVIDERS: dict[str, Callable[[Settings], STTProvider]] = {
    "google": google_provider.GoogleSTT,
    "azure": azure_provider.AzureSTT,
    "deepgram": deepgram_provider.DeepgramSTT,
}
TTS_PROVIDERS: dict[str, Callable[[Settings], TTSProvider]] = {
    "google": google_provider.GoogleTTS,
    "azure": azure_provider.AzureTTS,
    "elevenlabs": elevenlabs_provider.ElevenLabsTTS,
}


def create_stt(settings: Settings, name: str | None = None) -> STTProvider:
    """Raises MissingCredentialsError if the provider's keys or SDK are missing."""
    name = name or settings.stt_provider
    try:
        factory = STT_PROVIDERS[name]
    except KeyError:
        raise ValueError(
            f"unknown STT provider {name!r}; choose from {list(STT_PROVIDERS)}"
        ) from None
    return factory(settings)


def create_tts(settings: Settings, name: str | None = None) -> TTSProvider:
    """Raises MissingCredentialsError if the provider's keys or SDK are missing."""
    name = name or settings.tts_provider
    try:
        factory = TTS_PROVIDERS[name]
    except KeyError:
        raise ValueError(
            f"unknown TTS provider {name!r}; choose from {list(TTS_PROVIDERS)}"
        ) from None
    return factory(settings)


__all__ = ["MissingCredentialsError", "STT_PROVIDERS", "TTS_PROVIDERS", "create_stt", "create_tts"]
