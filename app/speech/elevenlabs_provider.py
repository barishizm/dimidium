"""ElevenLabs streaming text-to-speech (TTS only).

Lithuanian (checked 2026-10): listed for eleven_v3, eleven_v4 and eleven_v4_turbo; NOT for
eleven_flash_v2_5 / eleven_multilingual_v2. The docs only confirm eleven_v3 on the plain
Text-to-Speech API, so that is the default; try ELEVENLABS_MODEL=eleven_v4_turbo for lower
latency. Output is requested directly as ulaw_8000 (Twilio format).
"""

from collections.abc import AsyncIterator

import httpx

from app.config import Settings
from app.speech.base import MissingCredentialsError, SpeechError, TTSProvider, VoiceConfig

_API_BASE = "https://api.elevenlabs.io/v1"


def missing_credentials(settings: Settings) -> list[str]:
    missing: list[str] = []
    if settings.elevenlabs_api_key is None:
        missing.append("ELEVENLABS_API_KEY")
    if not settings.elevenlabs_voice_id:
        missing.append("ELEVENLABS_VOICE_ID")
    return missing


class ElevenLabsTTS(TTSProvider):
    name = "elevenlabs"

    def __init__(self, settings: Settings) -> None:
        missing = missing_credentials(settings)
        if missing:
            raise MissingCredentialsError(self.name, missing)
        assert settings.elevenlabs_api_key is not None
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=_API_BASE,
            headers={"xi-api-key": settings.elevenlabs_api_key.get_secret_value()},
            timeout=httpx.Timeout(settings.speech_timeout_seconds),
        )

    async def synthesize(self, text: str, voice: VoiceConfig) -> AsyncIterator[bytes]:
        voice_id = voice.voice or self._settings.elevenlabs_voice_id
        body: dict[str, object] = {
            "text": text,
            "model_id": self._settings.elevenlabs_model,
            "language_code": voice.language.split("-")[0],
        }
        if voice.speaking_rate != 1.0:
            body["voice_settings"] = {"speed": voice.speaking_rate}
        try:
            async with self._client.stream(
                "POST",
                f"/text-to-speech/{voice_id}/stream",
                params={"output_format": "ulaw_8000"},
                json=body,
            ) as response:
                if response.status_code >= 400:
                    detail = (await response.aread()).decode(errors="replace")[:300]
                    raise SpeechError(f"elevenlabs tts: HTTP {response.status_code}: {detail}")
                async for chunk in response.aiter_bytes():
                    if chunk:
                        yield chunk
        except httpx.HTTPError as exc:
            raise SpeechError(f"elevenlabs tts: {type(exc).__name__}: {exc}") from exc

    async def aclose(self) -> None:
        await self._client.aclose()
