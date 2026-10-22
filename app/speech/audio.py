"""Telephony audio helpers: G.711 mu-law codec, WAV wrapping, file loading for benchmarks."""

import io
import struct
import wave
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import numpy as np

from app.speech.base import TELEPHONY_FRAME_BYTES, TELEPHONY_SAMPLE_RATE

_MULAW_BIAS = 0x84
_MULAW_CLIP = 32635


def pcm16_to_mulaw(pcm: bytes) -> bytes:
    """Encode little-endian signed 16-bit PCM as G.711 mu-law."""
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.int32)
    sign = np.where(samples < 0, 0x80, 0)
    magnitude = np.minimum(np.abs(samples), _MULAW_CLIP) + _MULAW_BIAS
    exponent = np.floor(np.log2(magnitude >> 7)).astype(np.int32)
    mantissa = (magnitude >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa) & 0xFF).astype(np.uint8).tobytes()


def mulaw_to_pcm16(mulaw: bytes) -> bytes:
    """Decode G.711 mu-law to little-endian signed 16-bit PCM."""
    u = ~np.frombuffer(mulaw, dtype=np.uint8).astype(np.int32) & 0xFF
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    magnitude = (((mantissa << 3) + _MULAW_BIAS) << exponent) - _MULAW_BIAS
    return np.where(u & 0x80, -magnitude, magnitude).astype("<i2").tobytes()


def strip_wav_header(data: bytes) -> bytes:
    """Return the payload of the 'data' chunk if data is a RIFF/WAV file, else data unchanged."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return data
    pos = 12
    while pos + 8 <= len(data):
        chunk_id, size = struct.unpack("<4sI", data[pos : pos + 8])
        if chunk_id == b"data":
            return data[pos + 8 : pos + 8 + size]
        pos += 8 + size + (size & 1)
    return b""


def mulaw_to_wav(mulaw: bytes) -> bytes:
    """Wrap mu-law audio as a 16-bit PCM WAV file (plays everywhere, sounds like the phone)."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(TELEPHONY_SAMPLE_RATE)
        wav.writeframes(mulaw_to_pcm16(mulaw))
    return buffer.getvalue()


def iter_frames(data: bytes, frame_bytes: int = TELEPHONY_FRAME_BYTES) -> Iterator[bytes]:
    for start in range(0, len(data), frame_bytes):
        yield data[start : start + frame_bytes]


async def rechunk(chunks: AsyncIterator[bytes], max_bytes: int) -> AsyncIterator[bytes]:
    """Split chunks larger than max_bytes (some APIs cap the request size)."""
    async for chunk in chunks:
        for start in range(0, len(chunk), max_bytes):
            yield chunk[start : start + max_bytes]


def mulaw_duration_seconds(mulaw: bytes) -> float:
    return len(mulaw) / TELEPHONY_SAMPLE_RATE


def load_audio_as_mulaw(path: Path) -> bytes:
    """Load a wav/mp3/flac/ogg file and convert it to 8 kHz mono mu-law (simulated telephony).

    Needs the benchmark extras (soundfile, soxr): pip install -e ".[bench]".
    """
    import soundfile
    import soxr

    samples, rate = soundfile.read(str(path), dtype="float32", always_2d=True)
    mono = samples.mean(axis=1)
    if rate != TELEPHONY_SAMPLE_RATE:
        # Resampling to 8 kHz also low-passes at 4 kHz, like the phone network.
        mono = soxr.resample(mono, rate, TELEPHONY_SAMPLE_RATE, quality="HQ")
    pcm = (np.clip(mono, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    return pcm16_to_mulaw(pcm)
