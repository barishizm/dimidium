"""Write the generated hold music as an 8 kHz mono mu-law WAV file.

Use it as is, or as a reference for the format your own file must have. Then point
HOLD_AUDIO_PATH at the file in .env:

    python scripts/make_hold_audio.py                 # writes data/hold_music.wav
    python scripts/make_hold_audio.py --loops 4 --out data/my_hold.wav

To convert your own music (needs ffmpeg):

    ffmpeg -i music.mp3 -ar 8000 -ac 1 -c:a pcm_mulaw data/my_hold.wav
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.speech.base import TELEPHONY_SAMPLE_RATE  # noqa: E402
from app.telephony.hold_audio import (
    decode_hold_file,
    hold_music_mulaw,
    mulaw_wav_bytes,
)  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path("data/hold_music.wav"))
    parser.add_argument("--loops", type=int, default=1, help="repeat the 8 s loop this often")
    args = parser.parse_args()

    mulaw = hold_music_mulaw() * max(1, args.loops)
    data = mulaw_wav_bytes(mulaw)
    decode_hold_file(data)  # the app must be able to read what we wrote
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(data)
    seconds = len(mulaw) / TELEPHONY_SAMPLE_RATE
    print(f"Wrote {args.out} ({seconds:.0f} s, 8 kHz mono mu-law).")
    print(f"Use it with: HOLD_AUDIO_PATH={args.out}")


if __name__ == "__main__":
    main()
