"""Hold music: a generated soft, looping arpeggio inside the 300-3400 Hz phone band, or the
owner's own 8 kHz mu-law file (HOLD_AUDIO_PATH).

The generated loop needs no file and has no licensing question; scripts/make_hold_audio.py
writes it out as a starting point for a custom file.
"""

import logging
import struct
from functools import lru_cache
from pathlib import Path

import numpy as np

from app.speech.audio import mulaw_to_pcm16, pcm16_to_mulaw
from app.speech.base import TELEPHONY_SAMPLE_RATE

logger = logging.getLogger(__name__)

# C - Am - F - G, one bar each, four plucked notes per bar (Hz, all above the 300 Hz cutoff).
_PROGRESSION: tuple[tuple[float, ...], ...] = (
    (523.25, 659.25, 783.99, 659.25),
    (440.00, 523.25, 659.25, 523.25),
    (698.46, 880.00, 1046.50, 880.00),
    (783.99, 987.77, 1174.66, 987.77),
)
_NOTE_SECONDS = 0.5
_AMPLITUDE = 0.12  # about -18 dBFS: clearly "music on hold", never louder than speech
_FADE_SECONDS = 0.01
_WAVE_FORMAT_PCM = 1
_WAVE_FORMAT_MULAW = 7
# Shorter custom files would make the loop audibly repetitive (and are probably a mistake).
_MIN_CUSTOM_SECONDS = 1.0


def _note(frequency: float, sample_rate: int) -> np.ndarray:
    t = np.arange(int(_NOTE_SECONDS * sample_rate)) / sample_rate
    tone = np.sin(2 * np.pi * frequency * t) + 0.3 * np.sin(4 * np.pi * frequency * t)
    envelope = np.exp(-6 * t)
    fade = int(_FADE_SECONDS * sample_rate)
    envelope[:fade] *= np.linspace(0, 1, fade)
    envelope[-fade:] *= np.linspace(1, 0, fade)
    return tone * envelope


@lru_cache
def hold_music_pcm(sample_rate: int = TELEPHONY_SAMPLE_RATE) -> bytes:
    """One seamless loop (8 s) of 16-bit mono PCM."""
    notes = [_note(f, sample_rate) for bar in _PROGRESSION for f in bar]
    signal = np.concatenate(notes)
    signal = signal / np.max(np.abs(signal)) * _AMPLITUDE
    return (signal * 32767).astype("<i2").tobytes()


class HoldAudioError(ValueError):
    pass


def mulaw_wav_bytes(mulaw: bytes, sample_rate: int = TELEPHONY_SAMPLE_RATE) -> bytes:
    """Wrap mu-law audio in a WAV container (format 7, 8-bit, mono)."""
    fmt = struct.pack("<HHIIHHH", _WAVE_FORMAT_MULAW, 1, sample_rate, sample_rate, 1, 8, 0)
    body = (
        b"WAVE"
        + b"fmt "
        + struct.pack("<I", len(fmt))
        + fmt
        + b"fact"
        + struct.pack("<II", 4, len(mulaw))
        + b"data"
        + struct.pack("<I", len(mulaw))
        + mulaw
        + (b"\x00" if len(mulaw) % 2 else b"")
    )
    return b"RIFF" + struct.pack("<I", len(body)) + body


def decode_hold_file(data: bytes, raw: bool = False) -> bytes:
    """8 kHz mono audio (mu-law WAV, PCM16 WAV, or raw mu-law) -> 16-bit PCM at hold volume."""
    pcm = mulaw_to_pcm16(data) if raw else _decode_wav(data)
    if len(pcm) < _MIN_CUSTOM_SECONDS * TELEPHONY_SAMPLE_RATE * 2:
        raise HoldAudioError(f"shorter than {_MIN_CUSTOM_SECONDS:.0f} s")
    return _attenuate(pcm)


def _decode_wav(data: bytes) -> bytes:
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise HoldAudioError("not a WAV file")
    fmt: tuple[int, ...] | None = None
    pos = 12
    while pos + 8 <= len(data):
        chunk_id, size = struct.unpack("<4sI", data[pos : pos + 8])
        payload = data[pos + 8 : pos + 8 + size]
        if chunk_id == b"fmt ":
            fmt = struct.unpack("<HHIIHH", payload[:16])
        elif chunk_id == b"data":
            if fmt is None:
                raise HoldAudioError("data before fmt chunk")
            return _wav_payload_to_pcm(fmt, payload)
        pos += 8 + size + (size & 1)
    raise HoldAudioError("no audio data")


def _wav_payload_to_pcm(fmt: tuple[int, ...], payload: bytes) -> bytes:
    format_tag, channels, rate, _, _, bits = fmt
    if channels != 1 or rate != TELEPHONY_SAMPLE_RATE:
        raise HoldAudioError(f"must be 8000 Hz mono, not {rate} Hz x{channels}")
    if format_tag == _WAVE_FORMAT_MULAW and bits == 8:
        return mulaw_to_pcm16(payload)
    if format_tag == _WAVE_FORMAT_PCM and bits == 16:
        return payload[: len(payload) // 2 * 2]
    raise HoldAudioError(f"unsupported WAV encoding (format {format_tag}, {bits} bit)")


def _attenuate(pcm: bytes) -> bytes:
    """Scale down so the peak is at most the generated music's level (never boosts)."""
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    peak = np.max(np.abs(samples)) / 32767 if samples.size else 0.0
    if peak <= _AMPLITUDE:
        return pcm
    return (samples * (_AMPLITUDE / peak)).astype("<i2").tobytes()


@lru_cache
def load_hold_music(path: str | None) -> bytes:
    """The hold loop as 16-bit PCM: the custom file if it is usable, else generated music."""
    if not path:
        return hold_music_pcm()
    file = Path(path)
    try:
        return decode_hold_file(file.read_bytes(), raw=file.suffix.lower() in {".ulaw", ".raw"})
    except (OSError, HoldAudioError) as exc:
        logger.warning("HOLD_AUDIO_PATH %s unusable (%s); using generated music", path, exc)
        return hold_music_pcm()


def hold_music_mulaw() -> bytes:
    """The generated loop as raw 8 kHz mu-law (what goes down the phone line)."""
    return pcm16_to_mulaw(hold_music_pcm())
