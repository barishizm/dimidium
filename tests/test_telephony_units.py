import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import numpy as np
import pytest

from app.config import Settings
from app.db import init_db
from app.models import CallSession, CallStatus, TranscriptTurn
from app.speech.audio import mulaw_to_pcm16
from app.speech.base import SpeechError, TTSProvider, VoiceConfig
from app.telephony.agent_service import message_text
from app.telephony.call_handlers import CallRecorder
from app.telephony.hold_audio import hold_music_pcm
from app.telephony.latency import TurnLatencyTracker
from app.telephony.phrase_cache import PhraseCache, normalize_phrase
from app.telephony.speech_services import to_language, with_timeouts


class FakeTTS(TTSProvider):
    name = "fake"

    def __init__(self, fail: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self._fail = fail or set()

    async def synthesize(self, text: str, voice: VoiceConfig) -> AsyncIterator[bytes]:
        self.calls.append(text)
        if text in self._fail:
            raise SpeechError("boom")
        yield b"\xff" * 80
        yield b"\x7f" * 80


# --- settings ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "base, expected",
    [
        ("https://abc.ngrok-free.app/", "wss://abc.ngrok-free.app"),
        ("http://localhost:8000", "ws://localhost:8000"),
    ],
)
def test_public_ws_base_url(base: str, expected: str) -> None:
    assert Settings(_env_file=None, public_base_url=base).public_ws_base_url == expected


def test_stt_language_list_defaults_to_caller_language() -> None:
    assert Settings(_env_file=None).stt_language_list == ("lt-LT",)
    settings = Settings(_env_file=None, stt_languages="lt-LT, en-US")
    assert settings.stt_language_list == ("lt-LT", "en-US")


# --- phrase cache ------------------------------------------------------------------------


def test_normalize_phrase() -> None:
    assert normalize_phrase("  Laba   diena!\n") == "laba diena!"


async def test_warm_synthesizes_once_and_survives_restart(tmp_path: Path) -> None:
    tts = FakeTTS()
    cache = PhraseCache(tmp_path, "fake:voice")
    failed = await cache.warm(tts, VoiceConfig(), ["Labas.", "Ačiū.", "Labas."], timeout=1)
    assert failed == []
    assert tts.calls == ["Labas.", "Ačiū."]
    assert cache.get("labas.") == mulaw_to_pcm16(b"\xff" * 80 + b"\x7f" * 80)

    restarted = PhraseCache(tmp_path, "fake:voice")
    assert await restarted.warm(tts, VoiceConfig(), ["Labas."], timeout=1) == []
    assert tts.calls == ["Labas.", "Ačiū."]  # loaded from disk, not synthesized again
    assert "Labas." in restarted


async def test_warm_reports_failures_and_keys_disk_cache_by_voice(tmp_path: Path) -> None:
    cache = PhraseCache(tmp_path, "fake:voice")
    assert await cache.warm(FakeTTS(fail={"Blogai."}), VoiceConfig(), ["Blogai."], 1) == ["Blogai."]
    await cache.warm(FakeTTS(), VoiceConfig(), ["Labas."], timeout=1)
    other_voice = PhraseCache(tmp_path, "fake:other")
    assert not other_voice.load_from_disk("Labas.")


# --- hold music --------------------------------------------------------------------------


def test_hold_music_is_a_quiet_eight_second_loop() -> None:
    pcm = hold_music_pcm()
    samples = np.frombuffer(pcm, dtype="<i2")
    assert len(samples) == 8 * 8000
    assert 0.05 * 32767 < np.abs(samples).max() <= 0.13 * 32767
    assert abs(int(samples[0])) < 200 and abs(int(samples[-1])) < 200  # no click at the loop


# --- latency -----------------------------------------------------------------------------


def test_latency_tracker_measures_from_estimated_speech_end() -> None:
    now = [100.0]
    tracker = TurnLatencyTracker(clock=lambda: now[0])
    tracker.user_started()
    tracker.user_stopped(stop_secs=0.3, at=100.3)  # speech ended at 100.0
    now[0] = 100.5
    tracker.transcript()
    now[0] = 100.9
    tracker.llm_text()
    now[0] = 101.1
    turn = tracker.audio()
    assert turn is not None
    assert (turn.transcript_ms, turn.first_text_ms, turn.first_audio_ms) == (500, 900, 1100)
    assert tracker.audio() is None  # one measurement per caller turn
    assert tracker.summary() == {"turns": 1, "p50_ms": 1100, "max_ms": 1100}


def test_latency_tracker_ignores_audio_without_a_caller_turn() -> None:
    tracker = TurnLatencyTracker(clock=lambda: 5.0)
    assert tracker.audio() is None
    assert tracker.summary() == {"turns": 0}


# --- small helpers -----------------------------------------------------------------------


def test_message_text() -> None:
    assert message_text({"role": "user", "content": " Labas "}) == "Labas"
    parts = [{"type": "text", "text": "Labas"}, {"type": "image"}, {"type": "text", "text": "!"}]
    assert message_text({"role": "user", "content": parts}) == "Labas !"
    assert message_text({"role": "user"}) == ""


def test_to_language() -> None:
    assert to_language("lt-lt") is not None and to_language("lt-lt").value == "lt-LT"  # type: ignore[union-attr]
    assert to_language(None) is None
    assert to_language("xx-unknown") is None


async def test_with_timeouts_first_chunk() -> None:
    async def slow() -> AsyncIterator[bytes]:
        await asyncio.sleep(1)
        yield b"x"

    with pytest.raises(TimeoutError):
        _ = [c async for c in with_timeouts(slow(), 0.01, 1)]


async def test_with_timeouts_passes_chunks_and_skips_empty() -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for chunk in (b"a", b"", b"b"):
            yield chunk

    assert [c async for c in with_timeouts(chunks(), 1, 1)] == [b"a", b"b"]


# --- call recorder -----------------------------------------------------------------------


async def test_recorder_writes_call_and_only_new_turns(tmp_path: Path) -> None:
    db_path = str(tmp_path / "calls.db")
    await init_db(db_path)
    session = CallSession(call_sid="CA1", caller_number="+37060000000")
    recorder = CallRecorder(db_path, session)
    session.transcript.append(TranscriptTurn(speaker="agent", text="Laba diena"))
    await recorder.flush()
    session.transcript.append(TranscriptTurn(speaker="caller", text="Labas"))
    session.status = CallStatus.COMPLETED
    await recorder.flush()

    async with aiosqlite.connect(db_path) as db:
        turns = await (await db.execute("SELECT speaker, text FROM transcript_turns")).fetchall()
        status = await (await db.execute("SELECT status FROM calls")).fetchone()
    assert turns == [("agent", "Laba diena"), ("caller", "Labas")]
    assert status == ("completed",)


async def test_recorder_never_raises(tmp_path: Path) -> None:
    recorder = CallRecorder(str(tmp_path / "missing" / "no.db"), CallSession("CA1", "+370"))
    await recorder.flush()  # no tables, no directory: logged, not raised
