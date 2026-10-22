"""Deepgram streaming speech-to-text (STT only).

Lithuanian (checked 2026-10): "lt" is supported by nova-3 and nova-2; not by Flux. Deepgram's
Aura TTS has no Lithuanian voice, so there is no TTS provider here. Uses the raw WebSocket API
(the SDK changes often): Twilio's 8 kHz mu-law is accepted natively.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from urllib.parse import urlencode

from app.config import Settings
from app.speech.base import (
    TELEPHONY_SAMPLE_RATE,
    MissingCredentialsError,
    SpeechError,
    STTConfig,
    STTProvider,
    TranscriptEvent,
)

_LISTEN_URL = "wss://api.deepgram.com/v1/listen"


def missing_credentials(settings: Settings) -> list[str]:
    return [] if settings.deepgram_api_key is not None else ["DEEPGRAM_API_KEY"]


def listen_url(model: str, config: STTConfig) -> str:
    params = {
        "model": model,
        # Deepgram uses bare language codes ("lt"); only the primary language is sent.
        "language": config.languages[0].split("-")[0],
        "encoding": "mulaw",
        "sample_rate": TELEPHONY_SAMPLE_RATE,
        "channels": 1,
        "interim_results": str(config.interim_results).lower(),
        "punctuate": "true",
    }
    return f"{_LISTEN_URL}?{urlencode(params)}"


def parse_message(message: str, language: str) -> TranscriptEvent | None:
    data = json.loads(message)
    if data.get("type") != "Results":
        return None
    alternatives = (data.get("channel") or {}).get("alternatives") or []
    if not alternatives:
        return None
    text = (alternatives[0].get("transcript") or "").strip()
    if not text:
        return None
    return TranscriptEvent(
        text=text,
        is_final=bool(data.get("is_final")),
        language=language,
        confidence=alternatives[0].get("confidence"),
    )


class DeepgramSTT(STTProvider):
    name = "deepgram"

    def __init__(self, settings: Settings) -> None:
        missing = missing_credentials(settings)
        if missing:
            raise MissingCredentialsError(self.name, missing)
        assert settings.deepgram_api_key is not None
        self._api_key = settings.deepgram_api_key.get_secret_value()
        self._settings = settings

    async def stream(
        self, audio: AsyncIterator[bytes], config: STTConfig
    ) -> AsyncIterator[TranscriptEvent]:
        from websockets.asyncio.client import connect
        from websockets.exceptions import InvalidStatus, WebSocketException

        url = listen_url(self._settings.deepgram_model, config)
        try:
            async with connect(
                url,
                additional_headers={"Authorization": f"Token {self._api_key}"},
                open_timeout=self._settings.speech_timeout_seconds,
            ) as ws:

                async def pump() -> None:
                    async for chunk in audio:
                        await ws.send(chunk)
                    # Flushes the last results; the server then closes the socket.
                    await ws.send(json.dumps({"type": "CloseStream"}))

                pump_task = asyncio.create_task(pump())
                try:
                    async for message in ws:
                        if isinstance(message, bytes):
                            continue
                        event = parse_message(message, config.languages[0])
                        if event is not None:
                            yield event
                    if pump_task.done():
                        pump_task.result()
                finally:
                    pump_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await pump_task
        except InvalidStatus as exc:
            raise SpeechError(f"deepgram stt: HTTP {exc.response.status_code}") from exc
        except (OSError, TimeoutError, WebSocketException) as exc:
            raise SpeechError(f"deepgram stt: {type(exc).__name__}: {exc}") from exc
