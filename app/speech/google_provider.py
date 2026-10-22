"""Google Cloud Speech-to-Text v2 (Chirp) and Text-to-Speech.

Lithuanian support (checked 2026-10): STT lt-LT on chirp_3 (multi-regions eu/us) and on
chirp / chirp_2 (europe-west4); there is no "telephony" model for lt-LT. TTS lt-LT voices are
the Chirp3-HD family (streaming capable, e.g. lt-LT-Chirp3-HD-Kore) and lt-LT-Standard-B.

Measured 2026-10-22: chirp_3 streaming sends no interim results for lt-LT even with
interim_results=True, only a final one after each utterance.
"""

import importlib.util
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from app.config import Settings
from app.speech.audio import iter_frames, rechunk, strip_wav_header
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

# StreamingRecognize caps each audio request at 25 KB.
_MAX_STT_REQUEST_BYTES = 25_000
_ADC_FILE = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        return False


def missing_credentials(settings: Settings, sdk_module: str) -> list[str]:
    missing: list[str] = []
    if not _has_module(sdk_module):
        missing.append("google SDK packages (pip install -e '.[google]')")
    if not settings.google_cloud_project:
        missing.append("GOOGLE_CLOUD_PROJECT")
    path = settings.google_application_credentials
    if path:
        if not Path(path).expanduser().is_file():
            missing.append(f"GOOGLE_APPLICATION_CREDENTIALS (file not found: {path})")
    elif not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") and not _ADC_FILE.is_file():
        missing.append("GOOGLE_APPLICATION_CREDENTIALS")
    return missing


def _credentials(settings: Settings) -> Any:
    """Service-account credentials from the configured file, or None to use ADC."""
    if not settings.google_application_credentials:
        return None
    from google.oauth2 import service_account

    path = Path(settings.google_application_credentials).expanduser()
    return service_account.Credentials.from_service_account_file(str(path))


class GoogleSTT(STTProvider):
    name = "google"

    def __init__(self, settings: Settings) -> None:
        missing = missing_credentials(settings, "google.cloud.speech_v2")
        if missing:
            raise MissingCredentialsError(self.name, missing)
        self._settings = settings
        location = settings.google_stt_location
        self._endpoint = (
            "speech.googleapis.com" if location == "global" else f"{location}-speech.googleapis.com"
        )
        self._recognizer = (
            f"projects/{settings.google_cloud_project}/locations/{location}/recognizers/_"
        )
        self._client: Any = None

    def _get_client(self) -> Any:
        # Created lazily: the gRPC asyncio channel must be built inside the running loop.
        if self._client is None:
            from google.api_core.client_options import ClientOptions
            from google.cloud.speech_v2 import SpeechAsyncClient

            self._client = SpeechAsyncClient(
                credentials=_credentials(self._settings),
                client_options=ClientOptions(api_endpoint=self._endpoint),
            )
        return self._client

    async def stream(
        self, audio: AsyncIterator[bytes], config: STTConfig
    ) -> AsyncIterator[TranscriptEvent]:
        from google.api_core import exceptions as google_exceptions
        from google.cloud.speech_v2.types import cloud_speech as cs

        streaming_config = cs.StreamingRecognitionConfig(
            config=cs.RecognitionConfig(
                explicit_decoding_config=cs.ExplicitDecodingConfig(
                    encoding=cs.ExplicitDecodingConfig.AudioEncoding.MULAW,
                    sample_rate_hertz=TELEPHONY_SAMPLE_RATE,
                    audio_channel_count=1,
                ),
                language_codes=list(config.languages),
                model=self._settings.google_stt_model,
                features=cs.RecognitionFeatures(enable_automatic_punctuation=True),
            ),
            streaming_features=cs.StreamingRecognitionFeatures(
                interim_results=config.interim_results
            ),
        )

        async def requests() -> AsyncIterator[Any]:
            yield cs.StreamingRecognizeRequest(
                recognizer=self._recognizer, streaming_config=streaming_config
            )
            async for chunk in rechunk(audio, _MAX_STT_REQUEST_BYTES):
                yield cs.StreamingRecognizeRequest(audio=chunk)

        try:
            responses = await self._get_client().streaming_recognize(requests=requests())
            async for response in responses:
                interim: list[str] = []
                interim_language: str | None = None
                for result in response.results:
                    if not result.alternatives:
                        continue
                    alternative = result.alternatives[0]
                    text = alternative.transcript.strip()
                    if not text:
                        continue
                    if result.is_final:
                        yield TranscriptEvent(
                            text=text,
                            is_final=True,
                            language=result.language_code or None,
                            confidence=alternative.confidence or None,
                        )
                    else:
                        interim.append(text)
                        interim_language = interim_language or result.language_code or None
                if interim:
                    yield TranscriptEvent(
                        text=" ".join(interim), is_final=False, language=interim_language
                    )
        except google_exceptions.GoogleAPICallError as exc:
            raise SpeechError(f"google stt: {exc.code} {exc.message}") from exc

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.transport.close()
            self._client = None


class GoogleTTS(TTSProvider):
    name = "google"

    def __init__(self, settings: Settings) -> None:
        missing = missing_credentials(settings, "google.cloud.texttospeech")
        if missing:
            raise MissingCredentialsError(self.name, missing)
        self._settings = settings
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            from google.cloud import texttospeech

            self._client = texttospeech.TextToSpeechAsyncClient(
                credentials=_credentials(self._settings)
            )
        return self._client

    async def synthesize(self, text: str, voice: VoiceConfig) -> AsyncIterator[bytes]:
        from google.api_core import exceptions as google_exceptions

        name = voice.voice or self._settings.google_tts_voice
        try:
            # Only Chirp 3 HD voices support StreamingSynthesize.
            if "Chirp3-HD" in name:
                async for chunk in self._synthesize_streaming(text, name, voice):
                    yield chunk
            else:
                async for chunk in self._synthesize_unary(text, name, voice):
                    yield chunk
        except google_exceptions.GoogleAPICallError as exc:
            raise SpeechError(f"google tts: {exc.code} {exc.message}") from exc

    async def _synthesize_streaming(
        self, text: str, name: str, voice: VoiceConfig
    ) -> AsyncIterator[bytes]:
        from google.cloud import texttospeech as tts

        config = tts.StreamingSynthesizeConfig(
            voice=tts.VoiceSelectionParams(name=name, language_code=voice.language),
            streaming_audio_config=tts.StreamingAudioConfig(
                audio_encoding=tts.AudioEncoding.MULAW,
                sample_rate_hertz=TELEPHONY_SAMPLE_RATE,
                speaking_rate=voice.speaking_rate,
            ),
        )

        async def requests() -> AsyncIterator[Any]:
            yield tts.StreamingSynthesizeRequest(streaming_config=config)
            yield tts.StreamingSynthesizeRequest(input=tts.StreamingSynthesisInput(text=text))

        responses = await self._get_client().streaming_synthesize(
            requests=requests(), timeout=self._settings.speech_timeout_seconds
        )
        first = True
        async for response in responses:
            chunk = response.audio_content
            if first and chunk:
                chunk = strip_wav_header(chunk)
                first = False
            if chunk:
                yield chunk

    async def _synthesize_unary(
        self, text: str, name: str, voice: VoiceConfig
    ) -> AsyncIterator[bytes]:
        from google.cloud import texttospeech as tts

        response = await self._get_client().synthesize_speech(
            input=tts.SynthesisInput(text=text),
            voice=tts.VoiceSelectionParams(name=name, language_code=voice.language),
            audio_config=tts.AudioConfig(
                audio_encoding=tts.AudioEncoding.MULAW,
                sample_rate_hertz=TELEPHONY_SAMPLE_RATE,
                speaking_rate=voice.speaking_rate,
            ),
            timeout=self._settings.speech_timeout_seconds,
        )
        # Unary MULAW responses carry a WAV header.
        for frame in iter_frames(strip_wav_header(response.audio_content), 1600):
            yield frame

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.transport.close()
            self._client = None
