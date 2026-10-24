"""Transcript turns from GPT-Live transcript deltas, and the disclosure check.

GPT-Live sends transcript fragments without turn boundaries or a "done" event
(session.input_transcript.delta / session.output_transcript.delta), so a turn ends when the
other side starts talking or the call ends.
"""

from app.guards import fold
from app.models import Speaker, TranscriptTurn

# Folded (lowercase, no diacritics) word stems. "Atsiliepia dirbtinio intelekto asistentas...
# Pokalbis įrašomas ir transkribuojamas." and the English fallback both match.
_AI_MARKERS = ("dirbtin", "artificial intelligence", "ai assistant", "virtual assistant")
_RECORDING_MARKERS = ("irasom", "irasin", "transkrib", "record", "transcrib")


def disclosure_spoken(agent_text: str) -> bool:
    """True if the agent's speech says it is an AI and that the call is recorded/transcribed."""
    folded = fold(agent_text)
    return any(m in folded for m in _AI_MARKERS) and any(m in folded for m in _RECORDING_MARKERS)


class TranscriptAssembler:
    """Joins deltas into turns; a turn is complete when the speaker changes."""

    def __init__(self) -> None:
        self._speaker: Speaker | None = None
        self._parts: list[str] = []

    def add(self, speaker: Speaker, delta: str) -> TranscriptTurn | None:
        """Add a fragment; returns the previous speaker's turn if this one ended it."""
        done = self.flush() if speaker != self._speaker else None
        self._speaker = speaker
        self._parts.append(delta)
        return done

    def partial(self, speaker: Speaker) -> str:
        """The unfinished turn's text so far, if `speaker` is talking."""
        return "".join(self._parts).strip() if speaker == self._speaker else ""

    def flush(self) -> TranscriptTurn | None:
        speaker, text = self._speaker, "".join(self._parts).strip()
        self._speaker, self._parts = None, []
        if speaker is None or not text:
            return None
        return TranscriptTurn(speaker=speaker, text=text)
