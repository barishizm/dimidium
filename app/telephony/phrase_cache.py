"""Pre-synthesized fixed phrases (greeting, fillers, fallbacks), ready to play instantly.

Audio is kept as 8 kHz 16-bit PCM (Pipecat's pipeline format) and mirrored on disk as mu-law,
keyed by provider + voice + text, so restarts do not re-synthesize (or re-bill) anything.
"""

import asyncio
import hashlib
import logging
from collections.abc import Iterable
from pathlib import Path

from app.speech.audio import mulaw_to_pcm16
from app.speech.base import SpeechError, TTSProvider, VoiceConfig

logger = logging.getLogger(__name__)

_WARM_CONCURRENCY = 4


def normalize_phrase(text: str) -> str:
    return " ".join(text.split()).casefold()


class PhraseCache:
    def __init__(self, cache_dir: Path | None = None, voice_id: str = "") -> None:
        self._cache_dir = cache_dir
        # Identifies provider + voice, so switching voices never plays stale audio.
        self._voice_id = voice_id
        self._pcm: dict[str, bytes] = {}

    def get(self, text: str) -> bytes | None:
        return self._pcm.get(normalize_phrase(text))

    def put_mulaw(self, text: str, mulaw: bytes) -> None:
        self._pcm[normalize_phrase(text)] = mulaw_to_pcm16(mulaw)

    def __contains__(self, text: str) -> bool:
        return normalize_phrase(text) in self._pcm

    def __len__(self) -> int:
        return len(self._pcm)

    def _disk_path(self, text: str) -> Path | None:
        if self._cache_dir is None:
            return None
        digest = hashlib.sha256(f"{self._voice_id}|{normalize_phrase(text)}".encode()).hexdigest()
        return self._cache_dir / f"{digest[:24]}.ulaw"

    def load_from_disk(self, text: str) -> bool:
        path = self._disk_path(text)
        if path is None or not path.is_file():
            return False
        data = path.read_bytes()
        if not data:
            return False
        self.put_mulaw(text, data)
        return True

    def _save_to_disk(self, text: str, mulaw: bytes) -> None:
        path = self._disk_path(text)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(mulaw)
        except OSError:
            logger.warning("Could not write phrase cache file %s", path)

    async def warm(
        self, tts: TTSProvider, voice: VoiceConfig, texts: Iterable[str], timeout: float
    ) -> list[str]:
        """Load or synthesize every text. Returns the texts that could not be cached."""
        semaphore = asyncio.Semaphore(_WARM_CONCURRENCY)

        async def one(text: str) -> str | None:
            if text in self or self.load_from_disk(text):
                return None
            async with semaphore:
                try:
                    mulaw = await asyncio.wait_for(_synthesize_all(tts, text, voice), timeout)
                except (SpeechError, TimeoutError) as exc:
                    logger.warning("Phrase pre-synthesis failed: %s", exc)
                    return text
            if not mulaw:
                return text
            self.put_mulaw(text, mulaw)
            self._save_to_disk(text, mulaw)
            return None

        results = await asyncio.gather(*(one(text) for text in dict.fromkeys(texts)))
        return [text for text in results if text is not None]


async def _synthesize_all(tts: TTSProvider, text: str, voice: VoiceConfig) -> bytes:
    return b"".join([chunk async for chunk in tts.synthesize(text, voice)])
