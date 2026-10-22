"""Per-turn latency: end of caller speech -> first TTS audio, with a per-stage breakdown."""

import logging
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass

from pipecat.frames.frames import (
    AggregatedTextFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSTextFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TurnLatency:
    # All measured from the estimated end of speech (VAD stop time minus its stop_secs).
    transcript_ms: int | None
    first_text_ms: int | None
    first_audio_ms: int
    vad_stop_ms: int


class TurnLatencyTracker:
    """State machine fed with pipeline events; emits one TurnLatency per caller turn."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._speech_end: float | None = None
        self._vad_stop_secs = 0.0
        self._transcript_at: float | None = None
        self._text_at: float | None = None
        self.turns: list[TurnLatency] = []

    def user_started(self) -> None:
        self._speech_end = None
        self._transcript_at = None
        self._text_at = None

    def user_stopped(self, stop_secs: float, at: float | None = None) -> None:
        # VAD only reports "stopped" after stop_secs of silence; the speech ended before that.
        self._speech_end = (at if at is not None else self._clock()) - stop_secs
        self._vad_stop_secs = stop_secs
        self._text_at = None

    def transcript(self) -> None:
        # A final transcript may arrive before VAD stop (STT endpointing beat the VAD).
        self._transcript_at = self._clock()

    def llm_text(self) -> None:
        if self._speech_end is not None and self._text_at is None:
            self._text_at = self._clock()

    def audio(self) -> TurnLatency | None:
        if self._speech_end is None:
            return None
        end = self._speech_end
        self._speech_end = None

        def since_end(at: float | None) -> int | None:
            return None if at is None else max(0, round((at - end) * 1000))

        turn = TurnLatency(
            transcript_ms=since_end(self._transcript_at),
            first_text_ms=since_end(self._text_at),
            first_audio_ms=since_end(self._clock()) or 0,
            vad_stop_ms=round(self._vad_stop_secs * 1000),
        )
        self.turns.append(turn)
        return turn

    def summary(self) -> dict[str, int]:
        totals = [t.first_audio_ms for t in self.turns]
        if not totals:
            return {"turns": 0}
        return {
            "turns": len(totals),
            "p50_ms": round(statistics.median(totals)),
            "max_ms": max(totals),
        }


class TurnLatencyObserver(BaseObserver):
    def __init__(self, call_sid: str, target_ms: int) -> None:
        super().__init__(observe_every_push=False)
        self._call_sid = call_sid
        self._target_ms = target_ms
        self.tracker = TurnLatencyTracker()

    async def on_push_frame(self, data: FramePushed) -> None:
        frame = data.frame
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self.tracker.user_started()
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self.tracker.user_stopped(frame.stop_secs, at=frame.timestamp)
        elif isinstance(frame, TranscriptionFrame):
            self.tracker.transcript()
        elif isinstance(frame, LLMTextFrame | AggregatedTextFrame) and not isinstance(
            frame, TTSTextFrame
        ):
            # The agent service pushes pre-chunked sentences; TTSTextFrame is TTS's own echo.
            self.tracker.llm_text()
        elif isinstance(frame, TTSAudioRawFrame):
            turn = self.tracker.audio()
            if turn is not None:
                self._log(turn)

    def _log(self, turn: TurnLatency) -> None:
        level = logging.INFO if turn.first_audio_ms <= self._target_ms else logging.WARNING
        logger.log(
            level,
            "turn latency %d ms (target %d ms)",
            turn.first_audio_ms,
            self._target_ms,
            extra={
                "call_sid": self._call_sid,
                "latency_ms": turn.first_audio_ms,
                "stt_final_ms": turn.transcript_ms,
                "llm_first_text_ms": turn.first_text_ms,
                "vad_stop_ms": turn.vad_stop_ms,
                "target_met": turn.first_audio_ms <= self._target_ms,
            },
        )
