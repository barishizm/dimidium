"""Azure AI Speech: lt-LT speech-to-text and neural text-to-speech.

Voices (checked 2026-10): lt-LT-OnaNeural (female), lt-LT-LeonasNeural (male).
The Speech SDK is callback/thread based; events are bridged into asyncio queues.
"""

import asyncio
import importlib.util
from collections.abc import AsyncIterator
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from app.config import Settings
from app.speech.base import (
    TELEPHONY_SAMPLE_RATE,
    MissingCredentialsError,
    SpeechError,
    STTConfig,
    STTProvider,
    TranscriptEvent,
    TTSProvider,
    VoiceConfig,
)

_SDK_MODULE = "azure.cognitiveservices.speech"


def missing_credentials(settings: Settings) -> list[str]:
    missing: list[str] = []
    if not _sdk_available():
        missing.append("azure-cognitiveservices-speech package (pip install -e '.[azure]')")
    if settings.azure_speech_key is None:
        missing.append("AZURE_SPEECH_KEY")
    if not settings.azure_speech_region:
        missing.append("AZURE_SPEECH_REGION")
    return missing


def _sdk_available() -> bool:
    try:
        return importlib.util.find_spec(_SDK_MODULE) is not None
    except ModuleNotFoundError:
        return False


def _speech_config(settings: Settings) -> Any:
    import azure.cognitiveservices.speech as speechsdk

    assert settings.azure_speech_key is not None
    return speechsdk.SpeechConfig(
        subscription=settings.azure_speech_key.get_secret_value(),
        region=settings.azure_speech_region,
    )


def build_ssml(text: str, voice_name: str, voice: VoiceConfig) -> str:
    rate = f"{(voice.speaking_rate - 1.0) * 100:+.0f}%"
    return (
        f"<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' "
        f"xml:lang={quoteattr(voice.language)}>"
        f"<voice name={quoteattr(voice_name)}><prosody rate={quoteattr(rate)}>"
        f"{escape(text)}</prosody></voice></speak>"
    )


class AzureSTT(STTProvider):
    name = "azure"

    def __init__(self, settings: Settings) -> None:
        missing = missing_credentials(settings)
        if missing:
            raise MissingCredentialsError(self.name, missing)
        self._settings = settings

    async def stream(
        self, audio: AsyncIterator[bytes], config: STTConfig
    ) -> AsyncIterator[TranscriptEvent]:
        import azure.cognitiveservices.speech as speechsdk

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[TranscriptEvent | SpeechError | None] = asyncio.Queue()
        multilingual = len(config.languages) > 1

        speech_config = _speech_config(self._settings)
        stream_format = speechsdk.audio.AudioStreamFormat(
            samples_per_second=TELEPHONY_SAMPLE_RATE,
            bits_per_sample=8,
            channels=1,
            wave_stream_format=speechsdk.audio.AudioStreamWaveFormat.MULAW,
        )
        push_stream = speechsdk.audio.PushAudioInputStream(stream_format=stream_format)
        audio_config = speechsdk.audio.AudioConfig(stream=push_stream)
        if multilingual:
            speech_config.set_property(
                speechsdk.PropertyId.SpeechServiceConnection_LanguageIdMode, "Continuous"
            )
            recognizer = speechsdk.SpeechRecognizer(
                speech_config=speech_config,
                audio_config=audio_config,
                auto_detect_source_language_config=speechsdk.languageconfig.AutoDetectSourceLanguageConfig(
                    languages=list(config.languages)
                ),
            )
        else:
            recognizer = speechsdk.SpeechRecognizer(
                speech_config=speech_config,
                audio_config=audio_config,
                language=config.languages[0],
            )

        def put(item: TranscriptEvent | SpeechError | None) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, item)

        def language_of(result: Any) -> str | None:
            if multilingual:
                return speechsdk.AutoDetectSourceLanguageResult(result).language or None
            return config.languages[0]

        def on_recognizing(evt: Any) -> None:
            if config.interim_results and evt.result.text:
                put(TranscriptEvent(evt.result.text, False, language_of(evt.result)))

        def on_recognized(evt: Any) -> None:
            if evt.result.reason == speechsdk.ResultReason.RecognizedSpeech and evt.result.text:
                put(TranscriptEvent(evt.result.text, True, language_of(evt.result)))

        def on_canceled(evt: Any) -> None:
            # EndOfStream cancellations are normal; session_stopped follows them.
            if evt.reason == speechsdk.CancellationReason.Error:
                put(SpeechError(f"azure stt: {evt.error_code} {evt.error_details}"))

        recognizer.recognizing.connect(on_recognizing)
        recognizer.recognized.connect(on_recognized)
        recognizer.canceled.connect(on_canceled)
        recognizer.session_stopped.connect(lambda _evt: put(None))

        async def pump() -> None:
            try:
                async for chunk in audio:
                    push_stream.write(chunk)
            finally:
                push_stream.close()

        await loop.run_in_executor(None, recognizer.start_continuous_recognition)
        pump_task = asyncio.create_task(pump())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, SpeechError):
                    raise item
                yield item
            await pump_task
        finally:
            pump_task.cancel()
            await loop.run_in_executor(None, recognizer.stop_continuous_recognition)


class AzureTTS(TTSProvider):
    name = "azure"

    def __init__(self, settings: Settings) -> None:
        missing = missing_credentials(settings)
        if missing:
            raise MissingCredentialsError(self.name, missing)
        import azure.cognitiveservices.speech as speechsdk

        self._settings = settings
        speech_config = _speech_config(settings)
        speech_config.set_speech_synthesis_output_format(
            speechsdk.SpeechSynthesisOutputFormat.Raw8Khz8BitMonoMULaw
        )
        # One synthesizer per provider keeps the service connection warm between sentences.
        self._synthesizer = speechsdk.SpeechSynthesizer(
            speech_config=speech_config, audio_config=None
        )
        self._sink: tuple[asyncio.AbstractEventLoop, asyncio.Queue[Any]] | None = None
        self._lock = asyncio.Lock()

        def put(item: bytes | SpeechError | None) -> None:
            if self._sink is not None:
                sink_loop, sink_queue = self._sink
                sink_loop.call_soon_threadsafe(sink_queue.put_nowait, item)

        def on_canceled(evt: Any) -> None:
            details = speechsdk.SpeechSynthesisCancellationDetails(evt.result)
            put(SpeechError(f"azure tts: {details.error_code} {details.error_details}"))

        self._synthesizer.synthesizing.connect(lambda evt: put(evt.result.audio_data))
        self._synthesizer.synthesis_completed.connect(lambda _evt: put(None))
        self._synthesizer.synthesis_canceled.connect(on_canceled)

    async def synthesize(self, text: str, voice: VoiceConfig) -> AsyncIterator[bytes]:
        ssml = build_ssml(text, voice.voice or self._settings.azure_tts_voice, voice)
        timeout = self._settings.speech_timeout_seconds
        async with self._lock:
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue[bytes | SpeechError | None] = asyncio.Queue()
            self._sink = (loop, queue)
            self._synthesizer.speak_ssml_async(ssml)
            finished = False
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout)
                    except TimeoutError as exc:
                        raise SpeechError(f"azure tts: no audio for {timeout:.0f} s") from exc
                    if item is None:
                        finished = True
                        break
                    if isinstance(item, SpeechError):
                        finished = True
                        raise item
                    if item:
                        yield item
            finally:
                self._sink = None
                if not finished:
                    # Barge-in, consumer cancelled, or timeout: stop the in-flight synthesis.
                    self._synthesizer.stop_speaking_async()
