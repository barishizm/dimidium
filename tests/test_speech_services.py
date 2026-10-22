"""Pipecat STT/TTS services wrapping our providers, in a test pipeline with fake providers."""

import asyncio
from collections.abc import AsyncIterator

from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
)
from pipecat.tests.utils import SleepFrame, run_test

from app.speech.audio import mulaw_to_pcm16
from app.speech.base import (
    SpeechError,
    STTConfig,
    STTProvider,
    TranscriptEvent,
    TTSProvider,
    VoiceConfig,
)
from app.telephony.phrase_cache import PhraseCache
from app.telephony.speech_services import ProviderSTTService, ProviderTTSService

MULAW = b"\x10" * 160


class FakeTTS(TTSProvider):
    name = "fake"

    def __init__(self, failures: int = 0) -> None:
        self.calls: list[str] = []
        self._failures = failures

    async def synthesize(self, text: str, voice: VoiceConfig) -> AsyncIterator[bytes]:
        self.calls.append(text)
        if self._failures:
            self._failures -= 1
            raise SpeechError("tts down")
        yield MULAW


class Counter:
    def __init__(self) -> None:
        self.count = 0

    async def __call__(self) -> None:
        self.count += 1


def tts_service(provider: FakeTTS, cache: PhraseCache, failed: Counter) -> ProviderTTSService:
    return ProviderTTSService(
        provider,
        VoiceConfig(),
        cache,
        on_failure=failed,
        first_byte_timeout=1,
        chunk_timeout=1,
    )


def audio_of(frames: list[Frame]) -> bytes:
    return b"".join(f.audio for f in frames if isinstance(f, TTSAudioRawFrame))


async def test_cached_phrase_skips_the_provider() -> None:
    cache = PhraseCache()
    cache.put_mulaw("Laba diena!", b"\x20" * 1600)
    provider, failed = FakeTTS(), Counter()
    down, _ = await run_test(
        tts_service(provider, cache, failed),
        frames_to_send=[TTSSpeakFrame("Laba  diena!"), SleepFrame(sleep=0.1)],
        expected_down_frames=None,
    )
    assert provider.calls == []
    assert audio_of(list(down)) == mulaw_to_pcm16(b"\x20" * 1600)


async def test_live_synthesis_retries_once() -> None:
    provider, failed = FakeTTS(failures=1), Counter()
    down, _ = await run_test(
        tts_service(provider, PhraseCache(), failed),
        frames_to_send=[TTSSpeakFrame("Sveiki."), SleepFrame(sleep=0.1)],
        expected_down_frames=None,
    )
    assert provider.calls == ["Sveiki.", "Sveiki."]
    assert audio_of(list(down)) == mulaw_to_pcm16(MULAW)
    assert failed.count == 0


async def test_live_synthesis_reports_failure_after_retry() -> None:
    provider, failed = FakeTTS(failures=2), Counter()
    down, _ = await run_test(
        tts_service(provider, PhraseCache(), failed),
        frames_to_send=[TTSSpeakFrame("Sveiki."), SleepFrame(sleep=0.1)],
        expected_down_frames=None,
    )
    assert failed.count == 1
    assert audio_of(list(down)) == b""


class FakeSTT(STTProvider):
    """Each stream() call plays the next script: events to emit, or an exception."""

    name = "fake"

    def __init__(self, *scripts: list[TranscriptEvent] | Exception) -> None:
        self._scripts = list(scripts)
        self.audio: list[bytes] = []
        self.streams = 0

    async def stream(
        self, audio: AsyncIterator[bytes], config: STTConfig
    ) -> AsyncIterator[TranscriptEvent]:
        self.streams += 1
        script = self._scripts.pop(0) if self._scripts else []
        if isinstance(script, Exception):
            raise script
        self.audio.append(await anext(audio))
        for event in script:
            yield event
        await asyncio.sleep(10)  # stay open like a real stream


def pcm_frame() -> InputAudioRawFrame:
    return InputAudioRawFrame(audio=mulaw_to_pcm16(MULAW), sample_rate=8000, num_channels=1)


async def test_stt_pushes_interim_and_final_transcripts() -> None:
    provider = FakeSTT(
        [
            TranscriptEvent("Labas", is_final=False),
            TranscriptEvent("Labas rytas", is_final=True, language="lt-lt"),
        ]
    )
    failed = Counter()
    stt = ProviderSTTService(provider, STTConfig(), on_failure=failed, ttfs_p99_latency=1.0)
    down, _ = await run_test(
        stt, frames_to_send=[pcm_frame(), SleepFrame(sleep=0.2)], expected_down_frames=None
    )
    interim = [f for f in down if isinstance(f, InterimTranscriptionFrame)]
    final = [f for f in down if isinstance(f, TranscriptionFrame)]
    assert [f.text for f in interim] == ["Labas"]
    assert [f.text for f in final] == ["Labas rytas"]
    assert final[0].finalized and final[0].language == "lt-LT"
    assert provider.audio == [MULAW]  # PCM from the pipeline converted back to mu-law
    assert failed.count == 0


async def test_stt_retries_once_then_reports_failure() -> None:
    provider = FakeSTT(SpeechError("down"), SpeechError("still down"))
    failed = Counter()
    stt = ProviderSTTService(provider, STTConfig(), on_failure=failed, ttfs_p99_latency=1.0)
    await run_test(stt, frames_to_send=[SleepFrame(sleep=0.2)], expected_down_frames=None)
    assert provider.streams == 2
    assert failed.count == 1


async def test_stt_recovers_after_one_failure() -> None:
    provider = FakeSTT(SpeechError("blip"), [TranscriptEvent("Labas", is_final=True)])
    failed = Counter()
    stt = ProviderSTTService(provider, STTConfig(), on_failure=failed, ttfs_p99_latency=1.0)
    down, _ = await run_test(
        stt,
        frames_to_send=[SleepFrame(sleep=0.1), pcm_frame(), SleepFrame(sleep=0.2)],
        expected_down_frames=None,
    )
    assert [f.text for f in down if isinstance(f, TranscriptionFrame)] == ["Labas"]
    assert failed.count == 0
