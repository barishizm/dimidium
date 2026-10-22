"""Generated hold music: a soft, looping arpeggio that sits inside the 300-3400 Hz phone band.

Generated rather than shipped as a file, so there is no licensing question and nothing to load.
"""

from functools import lru_cache

import numpy as np

from app.speech.base import TELEPHONY_SAMPLE_RATE

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
