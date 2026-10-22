"""Pipecat STT/TTS services backed by our own provider interfaces (app/speech).

The live call uses the same Google / Azure / Deepgram / ElevenLabs code the speech benchmark
measured, chosen with STT_PROVIDER / TTS_PROVIDER. Pipecat's pipeline runs on 8 kHz 16-bit PCM;
the providers speak 8 kHz mu-law, so audio is converted at this boundary.
"""

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from typing import Any

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InterimTranscriptionFrame,
    StartFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
)
from pipecat.services.settings import STTSettings, TTSSettings
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601

from app.speech.audio import mulaw_to_pcm16, pcm16_to_mulaw
from app.speech.base import (
    TELEPHONY_SAMPLE_RATE,
    SpeechError,
    STTConfig,
    STTProvider,
    TranscriptEvent,
    TTSProvider,
    VoiceConfig,
)
from app.telephony.phrase_cache import PhraseCache

logger = logging.getLogger(__name__)

FailureCallback = Callable[[], Awaitable[None]]

# A stream that ends this soon without producing anything counts as a failed attempt.
_MIN_HEALTHY_STREAM_SECONDS = 2.0
# 40 ms of 8 kHz PCM16 per frame: small enough for prompt barge-in, large enough to be cheap.
_PCM_FRAME_BYTES = 640
_LANGUAGES = {language.value.casefold(): language for language in Language}


def to_language(code: str | None) -> Language | None:
    return _LANGUAGES.get(code.casefold()) if code else None


class ProviderSTTService(STTService):
    """Streams caller audio into an STTProvider for the whole call.

    The provider stream is reopened when it ends (providers cap stream length). A stream that
    errors is retried once; a second consecutive failure calls `on_failure`.
    """

    def __init__(
        self,
        provider: STTProvider,
        config: STTConfig,
        *,
        on_failure: FailureCallback,
        ttfs_p99_latency: float,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            sample_rate=TELEPHONY_SAMPLE_RATE,
            ttfs_p99_latency=ttfs_p99_latency,
            # Informational only: the provider is configured through our own Settings.
            settings=STTSettings(model=None, language=config.languages[0]),
            **kwargs,
        )
        self._provider = provider
        self._config = config
        self._on_failure = on_failure
        self._audio: asyncio.Queue[bytes | None] | None = None
        self._stream_task: asyncio.Task[None] | None = None

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        self._stream_task = self.create_task(self._run_streams(), name="stt-stream")

    async def stop(self, frame: EndFrame) -> None:
        await self._shutdown()
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame) -> None:
        await self._shutdown()
        await super().cancel(frame)

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        if self._audio is not None:
            self._audio.put_nowait(pcm16_to_mulaw(audio))
        yield None

    async def _shutdown(self) -> None:
        if self._audio is not None:
            self._audio.put_nowait(None)
        if self._stream_task is not None:
            await self.cancel_task(self._stream_task)
            self._stream_task = None
        await self._provider.aclose()

    async def _run_streams(self) -> None:
        failures = 0
        while True:
            queue: asyncio.Queue[bytes | None] = asyncio.Queue()
            self._audio = queue
            opened_at = time.monotonic()
            produced = False
            try:
                async for event in self._provider.stream(_drain(queue), self._config):
                    produced = True
                    failures = 0
                    await self._push_event(event)
                if produced or time.monotonic() - opened_at >= _MIN_HEALTHY_STREAM_SECONDS:
                    logger.info("STT stream ended; reopening")
                    continue
                failures += 1
                logger.warning("STT stream closed immediately (attempt %d)", failures)
            except Exception as exc:  # SpeechError, or anything an SDK raises unwrapped
                failures += 1
                logger.warning("STT stream failed (attempt %d): %s", failures, exc)
            if failures >= 2:
                self._audio = None
                await self._on_failure()
                return

    async def _push_event(self, event: TranscriptEvent) -> None:
        language = to_language(event.language)
        if event.is_final:
            await self.push_frame(
                TranscriptionFrame(
                    event.text,
                    self._user_id,
                    time_now_iso8601(),
                    language,
                    finalized=True,
                )
            )
        else:
            await self.push_frame(
                InterimTranscriptionFrame(event.text, self._user_id, time_now_iso8601(), language)
            )


async def _drain(queue: asyncio.Queue[bytes | None]) -> AsyncIterator[bytes]:
    while (chunk := await queue.get()) is not None:
        yield chunk


class ProviderTTSService(TTSService):
    """Speaks text with a TTSProvider; cached fixed phrases play without calling the provider.

    A request that fails before any audio is retried once. When the retry fails too, the
    sentence is dropped and `on_failure` is called (the agent then switches to cached phrases).
    """

    def __init__(
        self,
        provider: TTSProvider,
        voice: VoiceConfig,
        phrases: PhraseCache,
        *,
        on_failure: FailureCallback,
        first_byte_timeout: float,
        chunk_timeout: float,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            sample_rate=TELEPHONY_SAMPLE_RATE,
            push_start_frame=True,
            push_stop_frames=True,
            settings=TTSSettings(model=None, voice=voice.voice, language=voice.language),
            **kwargs,
        )
        self._provider = provider
        self._voice = voice
        self._phrases = phrases
        self._on_failure = on_failure
        self._first_byte_timeout = first_byte_timeout
        self._chunk_timeout = chunk_timeout

    def can_generate_metrics(self) -> bool:
        return True

    async def stop(self, frame: EndFrame) -> None:
        await super().stop(frame)
        await self._provider.aclose()

    async def cancel(self, frame: CancelFrame) -> None:
        await super().cancel(frame)
        await self._provider.aclose()

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame | None, None]:
        cached = self._phrases.get(text)
        if cached is not None:
            await self.stop_ttfb_metrics()
            for start in range(0, len(cached), _PCM_FRAME_BYTES):
                yield self._audio_frame(cached[start : start + _PCM_FRAME_BYTES], context_id)
            return

        for attempt in (1, 2):
            got_audio = False
            try:
                async for mulaw in with_timeouts(
                    self._provider.synthesize(text, self._voice),
                    self._first_byte_timeout,
                    self._chunk_timeout,
                ):
                    if not got_audio:
                        got_audio = True
                        await self.stop_ttfb_metrics()
                    yield self._audio_frame(mulaw_to_pcm16(mulaw), context_id)
                return
            except (SpeechError, TimeoutError) as exc:
                if got_audio:
                    # Repeating a half-spoken sentence would sound worse than cutting it.
                    logger.warning("TTS failed mid-sentence: %s", exc)
                    return
                logger.warning("TTS failed (attempt %d): %s", attempt, exc)
        await self._on_failure()

    def _audio_frame(self, pcm: bytes, context_id: str) -> TTSAudioRawFrame:
        return TTSAudioRawFrame(pcm, self.sample_rate, 1, context_id=context_id)


async def with_timeouts(
    chunks: AsyncIterator[bytes], first_timeout: float, next_timeout: float
) -> AsyncIterator[bytes]:
    """Iterate chunks, raising TimeoutError if the first or any later chunk is too slow."""
    iterator = aiter(chunks)
    timeout = first_timeout
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(anext(iterator), timeout)
            except StopAsyncIteration:
                return
            timeout = next_timeout
            if chunk:
                yield chunk
    finally:
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            await aclose()
