import json
import math
import struct

import numpy as np
import pytest

from app.config import Settings
from app.speech.audio import (
    iter_frames,
    mulaw_duration_seconds,
    mulaw_to_pcm16,
    mulaw_to_wav,
    pcm16_to_mulaw,
    strip_wav_header,
)
from app.speech.azure_provider import build_ssml
from app.speech.base import MissingCredentialsError, STTConfig, VoiceConfig
from app.speech.deepgram_provider import listen_url, parse_message
from app.speech.factory import create_stt, create_tts
from app.speech.metrics import error_rates, format_table, normalize_transcript, percentile
from app.speech.phrases_lt import PHRASES, PHRASES_BY_KEY


@pytest.fixture
def bare_settings(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Settings:
    """Settings with no speech credentials at all, independent of the developer's .env."""
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    return Settings(_env_file=None)


# --- mu-law codec ------------------------------------------------------------


def pcm(*samples: int) -> bytes:
    return np.array(samples, dtype="<i2").tobytes()


def test_mulaw_known_values() -> None:
    assert pcm16_to_mulaw(pcm(0, 32767, -32768)) == bytes([0xFF, 0x80, 0x00])
    assert mulaw_to_pcm16(bytes([0xFF, 0x80, 0x00])) == pcm(0, 32124, -32124)


def test_mulaw_roundtrip_of_every_code() -> None:
    codes = bytes(range(256))
    reencoded = pcm16_to_mulaw(mulaw_to_pcm16(codes))
    # 0x7F is "negative zero"; it decodes to 0, which encodes as 0xFF.
    expected = bytes(0xFF if code == 0x7F else code for code in codes)
    assert reencoded == expected


def test_mulaw_quantization_error_is_small() -> None:
    signal = (np.sin(np.linspace(0, 20 * np.pi, 8000)) * 20000).astype("<i2")
    decoded = np.frombuffer(mulaw_to_pcm16(pcm16_to_mulaw(signal.tobytes())), dtype="<i2")
    relative = np.abs(decoded.astype(int) - signal.astype(int)) / (np.abs(signal) + 64)
    assert relative.max() < 0.07


# --- WAV helpers -------------------------------------------------------------


def test_strip_wav_header_returns_data_chunk() -> None:
    payload = b"\x01\x02\x03"
    fmt = struct.pack("<4sIHHIIHH", b"fmt ", 16, 7, 1, 8000, 8000, 1, 8)
    extra = b"LIST" + struct.pack("<I", 3) + b"abc\x00"  # odd size, padded
    data = b"data" + struct.pack("<I", len(payload)) + payload
    body = b"WAVE" + fmt + extra + data
    wav = b"RIFF" + struct.pack("<I", len(body)) + body
    assert strip_wav_header(wav) == payload


def test_strip_wav_header_passes_raw_audio_through() -> None:
    assert strip_wav_header(b"\xff\xfe\xfd") == b"\xff\xfe\xfd"


def test_mulaw_to_wav_is_playable_pcm() -> None:
    wav = mulaw_to_wav(b"\xff" * 800)
    assert wav[:4] == b"RIFF"
    assert len(strip_wav_header(wav)) == 1600  # 16-bit samples


def test_frames_and_duration() -> None:
    audio = b"\xff" * 400
    assert [len(f) for f in iter_frames(audio)] == [160, 160, 80]
    assert mulaw_duration_seconds(audio) == 0.05


# --- metrics -----------------------------------------------------------------


def test_normalize_keeps_lithuanian_letters() -> None:
    text = "Laba diena!  Čia kurjeris iš DPD, ar ŽINUTĘ   galiu palikti?"
    assert normalize_transcript(text) == "laba diena čia kurjeris iš dpd ar žinutę galiu palikti"


def test_error_rates_ignore_case_and_punctuation() -> None:
    wer, cer = error_rates(["Laba diena, čia kurjeris."], ["laba diena čia kurjeris"])
    assert wer == 0 and cer == 0


def test_error_rates_count_word_errors() -> None:
    wer, _ = error_rates(["laba diena čia kurjeris"], ["laba diena čia kurjerė"])
    assert wer == pytest.approx(0.25)


def test_error_rates_empty_hypothesis_is_full_error() -> None:
    wer, cer = error_rates(["laba diena"], [""])
    assert wer == 1 and cer == 1


def test_error_rates_without_references_is_nan() -> None:
    assert all(math.isnan(v) for v in error_rates([], []))


def test_percentile() -> None:
    assert percentile([3, 1, 2], 50) == 2
    assert percentile([1, 2, 3, 4], 90) == pytest.approx(3.7)
    assert math.isnan(percentile([], 50))


def test_format_table_aligns_columns() -> None:
    table = format_table(["a", "bb"], [["xxx", "y"]])
    assert table.splitlines() == ["a    bb", "---  --", "xxx  y"]


# --- providers / factory -----------------------------------------------------


@pytest.mark.parametrize("name", ["google", "azure", "deepgram"])
def test_create_stt_without_credentials_raises(bare_settings: Settings, name: str) -> None:
    with pytest.raises(MissingCredentialsError) as info:
        create_stt(bare_settings, name)
    assert info.value.provider == name
    assert info.value.missing


@pytest.mark.parametrize("name", ["google", "azure", "elevenlabs"])
def test_create_tts_without_credentials_raises(bare_settings: Settings, name: str) -> None:
    with pytest.raises(MissingCredentialsError):
        create_tts(bare_settings, name)


def test_missing_credentials_name_the_env_vars(bare_settings: Settings) -> None:
    with pytest.raises(MissingCredentialsError) as info:
        create_tts(bare_settings, "elevenlabs")
    assert info.value.missing == ["ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID"]
    with pytest.raises(MissingCredentialsError) as info:
        create_stt(bare_settings, "google")
    assert "GOOGLE_CLOUD_PROJECT" in info.value.missing
    assert "GOOGLE_APPLICATION_CREDENTIALS" in info.value.missing


def test_unknown_provider_is_rejected(bare_settings: Settings) -> None:
    with pytest.raises(ValueError):
        create_stt(bare_settings, "elevenlabs")  # TTS-only provider


def test_default_provider_comes_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STT_PROVIDER", "deepgram")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-key")
    assert create_stt(Settings(_env_file=None)).name == "deepgram"


def test_deepgram_listen_url() -> None:
    url = listen_url("nova-3", STTConfig(languages=("lt-LT", "en-US")))
    assert "model=nova-3" in url
    assert "language=lt&" in url
    assert "encoding=mulaw" in url and "sample_rate=8000" in url
    assert "interim_results=true" in url


def test_deepgram_parse_message() -> None:
    message = json.dumps(
        {
            "type": "Results",
            "is_final": True,
            "channel": {"alternatives": [{"transcript": " Laba diena ", "confidence": 0.9}]},
        }
    )
    event = parse_message(message, "lt-LT")
    assert event is not None
    assert (event.text, event.is_final, event.confidence) == ("Laba diena", True, 0.9)
    empty = json.dumps({"type": "Results", "channel": {"alternatives": [{"transcript": ""}]}})
    assert parse_message(empty, "lt-LT") is None
    assert parse_message(json.dumps({"type": "Metadata"}), "lt-LT") is None


def test_azure_ssml_escapes_text_and_sets_rate() -> None:
    ssml = build_ssml("A & B <c>", "lt-LT-OnaNeural", VoiceConfig(speaking_rate=1.1))
    assert "A &amp; B &lt;c&gt;" in ssml
    assert 'name="lt-LT-OnaNeural"' in ssml
    assert 'rate="+10%"' in ssml
    assert 'xml:lang="lt-LT"' in ssml


# --- phrases -----------------------------------------------------------------


def test_phrase_keys_are_unique() -> None:
    assert len(PHRASES_BY_KEY) == len(PHRASES)


def test_required_phrases_present() -> None:
    for key in (
        "greeting",
        "hold",
        "still_trying",
        "unavailable_take_message",
        "message_taken",
        "technical_issue",
        "goodbye",
        "repeat",
        "filler",
        "filler_long",
        "are_you_there",
        "cant_hear",
        "time_limit",
    ):
        assert key in PHRASES_BY_KEY


def test_greeting_discloses_ai_and_recording() -> None:
    greeting = PHRASES_BY_KEY["greeting"].text
    assert "dirbtinio intelekto" in greeting
    assert "įrašomas" in greeting


def test_phrases_are_short_plain_speech() -> None:
    for phrase in PHRASES:
        assert phrase.text and phrase.english
        assert len(phrase.text) < 220, phrase.key
        assert not any(char in phrase.text for char in "*#_[]\n"), phrase.key
