"""The pipeline stage that talks: AgentBrain (Anthropic tool use) inside a Pipecat pipeline.

It stands where an LLMService would. It reads the finished caller turn from the user
aggregator's LLMContextFrame and pushes LLMFullResponseStart, one AggregatedTextFrame per
sentence (sent to TTS immediately, no re-aggregation) and LLMFullResponseEnd. The conversation
history lives in AgentBrain, not in Pipecat's context, because the brain owns the tool loop.

Beyond replying, this stage keeps the line from going silent and the call bounded:
- the disclosure greeting is spoken from code (pre-synthesized), never left to the model;
- filler phrases when the model is slow; a stuck turn counts as an LLM failure;
- hold music and "still trying" phrases while the owner is asked; what the caller says on
  hold is buffered (never an LLM turn), answered with a canned phrase, and handed to the
  model together with the owner's decision;
- after two LLM failures, or when TTS fails, it takes a message using cached phrases only;
- call-length, turn and caller-silence limits.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing, asynccontextmanager, suppress
from enum import StrEnum
from typing import Any

from pipecat.frames.frames import (
    AggregatedTextFrame,
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    EndWorkerFrame,
    Frame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    OutputAudioRawFrame,
    StartFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.utils.text.base_text_aggregator import AggregationType

from app.agent.brain import AgentBrain
from app.config import Settings
from app.models import CallSession, TranscriptTurn
from app.speech.base import TELEPHONY_SAMPLE_RATE
from app.speech.phrases_lt import PHRASES_BY_KEY
from app.telephony.hold_audio import hold_music_pcm

logger = logging.getLogger(__name__)

WRAP_UP_EVENT = (
    "the call is about to reach its length limit. Wrap up now: if you still need the owner's "
    "decision, take a message with save_message instead. Say goodbye and call end_call."
)
_MONITOR_INTERVAL = 0.5
_HOLD_CHUNK_SECONDS = 0.1
# How far hold audio may run ahead of real time (the rest is cut off when the hold ends).
_HOLD_LEAD_SECONDS = 0.3
# Wait at most this long for "please hold" to finish before hold music starts.
_HOLD_SPEECH_WAIT = 8.0
# At most one canned "yes, I am here" per this many seconds, however much the caller talks.
_HOLD_REPLY_MIN_GAP = 6.0
_MAX_IDLE_PROMPTS = 2


def phrase(key: str) -> str:
    return PHRASES_BY_KEY[key].text


class Mode(StrEnum):
    AGENT = "agent"  # the model is talking
    MESSAGE = "message"  # fallback: cached phrases only, the next caller turn is the message
    CLOSING = "closing"  # goodbye is queued, the pipeline is ending


class CallLimits:
    def __init__(self, settings: Settings) -> None:
        self.max_call_seconds = settings.max_call_seconds
        self.max_caller_turns = settings.max_caller_turns
        self.wrap_up_seconds = settings.wrap_up_seconds
        self.filler_after_seconds = settings.filler_after_seconds
        self.max_fillers_per_turn = settings.max_fillers_per_turn
        self.caller_idle_seconds = settings.caller_idle_seconds
        self.hold_phrase_interval_seconds = settings.hold_phrase_interval_seconds


class AgentBrainService(FrameProcessor):
    def __init__(
        self,
        *,
        session: CallSession,
        limits: CallLimits,
        hold_phrase_audio: Callable[[str], bytes | None],
        persist: Callable[[], Awaitable[None]],
        hold_music: bytes | None = None,
        greeting_key: str = "greeting",
        clock: Callable[[], float] = time.monotonic,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._session = session
        self._greeting_key = greeting_key
        self._limits = limits
        # Phrase key -> cached 8 kHz PCM (None if not cached: then nothing is played).
        self._hold_phrase_audio = hold_phrase_audio
        self._hold_music = hold_music or hold_music_pcm()
        self._persist = persist
        self._clock = clock
        self._brain: AgentBrain | None = None
        # Delivered to the owner by the call runner once the pipeline has ended:
        # (purpose, details) for the message store, and plain alerts.
        self.owner_notes: list[tuple[str, str]] = []
        self.owner_alerts: list[str] = []

        self.mode = Mode.AGENT
        self.end_reason: str | None = None
        self.on_hold = False
        self._end_requested: str | None = None
        self._turn_task: asyncio.Task[None] | None = None
        self._filler_task: asyncio.Task[None] | None = None
        self._hold_task: asyncio.Task[None] | None = None
        # Caller utterances heard during the current hold, and a pending canned reply.
        self._hold_heard: list[str] = []
        self._hold_reply_due = False
        self._last_hold_reply: float | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._response_open = False
        self._started_at: float | None = None
        self._caller_turns = 0
        self._llm_failures = 0
        self._user_messages_seen = 0
        self._bot_speaking = False
        self._bot_stopped = asyncio.Event()
        self._user_speaking = False
        self._last_activity = clock()
        self._idle_prompts = 0

    def attach(self, brain: AgentBrain) -> None:
        self._brain = brain

    @property
    def user_muted(self) -> bool:
        """Caller audio must not start turns or interrupt: on hold, or saying goodbye."""
        return self.on_hold or self.mode is Mode.CLOSING

    # --- lifecycle ---------------------------------------------------------------------------

    async def begin(self) -> None:
        """The pipeline has started: speak the disclosure greeting and start the call clock."""
        self._started_at = self._clock()
        self._last_activity = self._started_at
        greeting = phrase(self._greeting_key)
        if self._brain is not None:
            self._brain.add_opening(greeting)
        await self._speak(greeting)
        self._monitor_task = self.create_task(self._monitor(), name="call-monitor")
        await self._persist()

    def request_end(self, reason: str) -> None:
        """The agent called end_call; the pipeline ends once the current turn is spoken."""
        self._end_requested = reason

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMContextFrame):
            if not getattr(frame, "speculation", False):
                text = self._new_caller_text(frame.context)
                if text:
                    await self._on_caller_turn(text)
            return  # consumed, like an LLM service
        if isinstance(frame, InterruptionFrame):
            await self._on_interruption()
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
            self._bot_stopped.clear()
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            self._bot_stopped.set()
            self._last_activity = self._clock()
        elif isinstance(frame, UserStartedSpeakingFrame):
            self._user_speaking = True
            self._last_activity = self._clock()
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self._user_speaking = False
            self._last_activity = self._clock()
        elif isinstance(frame, EndFrame | CancelFrame):
            await self._stop_tasks()
        await self.push_frame(frame, direction)
        if isinstance(frame, StartFrame):
            # Behind the StartFrame, so TTS and the transport are running when it arrives.
            await self.begin()

    async def cleanup(self) -> None:
        await self._stop_tasks()
        await super().cleanup()

    async def _stop_tasks(self) -> None:
        for task in (self._monitor_task, self._filler_task, self._hold_task, self._turn_task):
            if task is not None and task is not asyncio.current_task() and not task.done():
                await self.cancel_task(task)

    # --- caller turns ------------------------------------------------------------------------

    def _new_caller_text(self, context: LLMContext) -> str:
        user_messages = [m for m in context.get_messages() if m.get("role") == "user"]
        new = user_messages[self._user_messages_seen :]
        self._user_messages_seen = len(user_messages)
        return " ".join(filter(None, (message_text(m) for m in new))).strip()

    async def _on_caller_turn(self, text: str) -> None:
        self._last_activity = self._clock()
        self._idle_prompts = 0
        if self.mode is Mode.CLOSING or self._brain is None:
            return
        if self.on_hold:
            # A turn that completed just as the hold began: never cancel the pending ask.
            self.on_hold_utterance(text)
            return
        if self.mode is Mode.MESSAGE:
            await self._take_message(text)
            return
        self._caller_turns += 1
        if self._caller_turns > self._limits.max_caller_turns:
            await self._close_for_limit("turn limit")
            return
        await self._cancel(self._turn_task)
        self._turn_task = self.create_task(self._run_turn(text, self._wrap_up_event()), "turn")

    def _wrap_up_event(self) -> str | None:
        near_time = self._elapsed() >= self._limits.max_call_seconds - self._limits.wrap_up_seconds
        near_turns = self._caller_turns >= self._limits.max_caller_turns - 1
        return WRAP_UP_EVENT if near_time or near_turns else None

    async def _run_turn(self, text: str, event: str | None) -> None:
        assert self._brain is not None
        self._arm_filler()
        try:
            # aclosing: when this task is cancelled (barge-in), the brain repairs its history
            # right away, before the next caller turn can start.
            async with aclosing(self._brain.respond(text, event)) as replies:
                async for sentence in replies:
                    self._disarm_filler()
                    await self._say_sentence(sentence)
            await self._close_response()
        finally:
            self._disarm_filler()
        await self._persist()

        if self._brain.last_turn_failed:
            await self._on_llm_failure(fallback_spoken=True)
        else:
            self._llm_failures = 0
        if self._brain.ended or self._end_requested:
            await self._close(self._end_requested or "agent ended the call")

    async def _on_interruption(self) -> None:
        """Barge-in: Pipecat already stops TTS and drops queued audio; stop generating too."""
        if self.on_hold or self.mode is not Mode.AGENT:
            return
        self._response_open = False
        await self._cancel(self._turn_task)
        self._disarm_filler()

    async def _on_llm_failure(self, fallback_spoken: bool) -> None:
        """First failure: the fallback phrase was spoken and the next turn retries. Second: take
        a message without the model."""
        self._llm_failures += 1
        logger.warning("LLM turn failed (%d in a row)", self._llm_failures)
        if not fallback_spoken:
            await self._speak(phrase("technical_issue"), record=True)
        if self._llm_failures >= 2 and self.mode is Mode.AGENT:
            # technical_issue already invited the caller to leave a message.
            self.mode = Mode.MESSAGE

    # --- speaking ----------------------------------------------------------------------------

    async def _say_sentence(self, sentence: str) -> None:
        if not self._response_open:
            await self.push_frame(LLMFullResponseStartFrame())
            self._response_open = True
        await self.push_frame(
            AggregatedTextFrame(text=sentence, aggregated_by=AggregationType.SENTENCE)
        )

    async def _close_response(self) -> None:
        if self._response_open:
            self._response_open = False
            await self.push_frame(LLMFullResponseEndFrame())

    async def _speak(self, text: str, record: bool = False) -> None:
        """Speak a fixed phrase (cached audio when available). Not added to Pipecat's context."""
        if record:
            self._session.transcript.append(TranscriptTurn(speaker="agent", text=text))
        await self.push_frame(TTSSpeakFrame(text, append_to_context=False))

    # --- silence guard -----------------------------------------------------------------------

    def _arm_filler(self) -> None:
        self._disarm_filler()
        if self.mode is Mode.AGENT:
            self._filler_task = self.create_task(self._filler_loop(), name="filler")

    def _disarm_filler(self) -> None:
        task, self._filler_task = self._filler_task, None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    async def _filler_loop(self) -> None:
        fillers = [phrase("filler"), phrase("filler_long")]
        for index in range(self._limits.max_fillers_per_turn):
            await asyncio.sleep(self._limits.filler_after_seconds)
            await self._speak(fillers[min(index, len(fillers) - 1)])
        await asyncio.sleep(self._limits.filler_after_seconds)
        if self.on_hold or self.mode is not Mode.AGENT:
            return
        logger.warning("Agent turn produced no speech in time; treating it as an LLM failure")
        # Detach first: the cancelled turn's cleanup disarms the filler, which would be us.
        self._filler_task = None
        await self._cancel(self._turn_task)
        await self._close_response()
        await self._on_llm_failure(fallback_spoken=False)

    # --- hold --------------------------------------------------------------------------------

    @asynccontextmanager
    async def hold(self) -> AsyncIterator[list[str]]:
        """Caller on hold while the owner is asked: music plus a periodic phrase, caller muted.

        Yields the list that collects what the caller says during the hold.
        """
        self._disarm_filler()
        await self._close_response()
        self.on_hold = True
        heard: list[str] = []
        self._hold_heard = heard
        self._hold_reply_due = False
        self._hold_task = self.create_task(self._play_hold(), name="hold")
        try:
            yield heard
        finally:
            self.on_hold = False
            await self._cancel(self._hold_task)
            self._hold_task = None
            # Drop the hold audio still queued, so the decision is relayed right away.
            await self.broadcast_interruption()
            self._last_activity = self._clock()
            if self.mode is Mode.AGENT:
                self._arm_filler()

    def on_hold_utterance(self, text: str) -> None:
        """The caller spoke during the hold: keep it for the model, answer with a canned phrase."""
        text = text.strip()
        if not text or not self.on_hold:
            return
        self._hold_heard.append(text)
        self._session.transcript.append(TranscriptTurn(speaker="caller", text=text))
        now = self._clock()
        if self._last_hold_reply is None or now - self._last_hold_reply >= _HOLD_REPLY_MIN_GAP:
            self._last_hold_reply = now
            self._hold_reply_due = True

    async def _play_hold(self) -> None:
        # Let "please hold" finish first, so the music does not talk over it.
        if self._bot_speaking:
            with suppress(TimeoutError):
                await asyncio.wait_for(self._bot_stopped.wait(), _HOLD_SPEECH_WAIT)
        music = self._hold_music
        chunk = int(TELEPHONY_SAMPLE_RATE * _HOLD_CHUNK_SECONDS) * 2
        started = self._clock()
        queued_seconds = 0.0
        next_phrase_at = self._limits.hold_phrase_interval_seconds
        position = 0
        while True:
            key = None
            if self._hold_reply_due:
                self._hold_reply_due = False
                key = "hold_still_here"
            elif queued_seconds >= next_phrase_at:
                key = "still_trying"
            if key is not None:
                # Any phrase restarts the interval, so two phrases never play back to back.
                next_phrase_at = queued_seconds + self._limits.hold_phrase_interval_seconds
                pcm = self._hold_phrase_audio(key)
                if pcm:
                    await self._push_audio(pcm)
                    queued_seconds += len(pcm) / (2 * TELEPHONY_SAMPLE_RATE)
            piece = music[position : position + chunk]
            position = (position + chunk) % len(music)
            await self._push_audio(piece)
            queued_seconds += len(piece) / (2 * TELEPHONY_SAMPLE_RATE)
            ahead = queued_seconds - (self._clock() - started)
            if ahead > _HOLD_LEAD_SECONDS:
                await asyncio.sleep(ahead - _HOLD_LEAD_SECONDS)

    async def _push_audio(self, pcm: bytes) -> None:
        await self.push_frame(OutputAudioRawFrame(pcm, TELEPHONY_SAMPLE_RATE, 1))

    # --- limits, idle caller and fallbacks ---------------------------------------------------

    def _elapsed(self) -> float:
        return 0.0 if self._started_at is None else self._clock() - self._started_at

    async def _monitor(self) -> None:
        while self.mode is not Mode.CLOSING:
            await asyncio.sleep(_MONITOR_INTERVAL)
            if self.on_hold:
                continue
            if self._elapsed() >= self._limits.max_call_seconds:
                await self._close_for_limit("call length limit")
                return
            if self._caller_is_idle():
                if self._idle_prompts >= _MAX_IDLE_PROMPTS:
                    await self._close("caller silent", ["goodbye"])
                    return
                self._idle_prompts += 1
                self._last_activity = self._clock()
                await self._speak(phrase("are_you_there"))

    def _caller_is_idle(self) -> bool:
        turn_running = self._turn_task is not None and not self._turn_task.done()
        busy = self._bot_speaking or self._user_speaking or turn_running
        return not busy and self._clock() - self._last_activity >= self._limits.caller_idle_seconds

    async def end_for_shutdown(self) -> None:
        """The server is stopping: apologize, promise to pass everything on, say goodbye."""
        if self.mode is not Mode.CLOSING:
            await self._close_for_limit("server shutdown")

    async def _close_for_limit(self, reason: str) -> None:
        logger.info("Ending call: %s", reason)
        self.owner_notes.append((f"Call cut off ({reason})", self._caller_lines()))
        await self._close(reason, ["time_limit", "goodbye"])

    async def on_stt_failure(self) -> None:
        """Speech recognition is down for good: the caller cannot be heard, so end politely."""
        if self.mode is Mode.CLOSING:
            return
        self.owner_alerts.append(
            "Konuşma tanıma (STT) çalışmadı; arayanı duyamadım. Lütfen geri arayın."
        )
        await self._close("speech recognition failed", ["cant_hear", "goodbye"])

    async def on_tts_failure(self) -> None:
        """Live TTS is down: continue with cached phrases only and take a message."""
        if self.mode is not Mode.AGENT:
            return
        logger.warning("TTS failed; switching to message-taking mode")
        self.mode = Mode.MESSAGE
        await self._cancel(self._turn_task)
        self._disarm_filler()
        await self._close_response()
        await self._speak(phrase("technical_issue"), record=True)

    async def _take_message(self, text: str) -> None:
        self._session.transcript.append(TranscriptTurn(speaker="caller", text=text))
        self.owner_notes.append(("Message taken after a technical problem", self._caller_lines()))
        await self._close("message taken", ["message_taken", "goodbye"])

    def _caller_lines(self) -> str:
        lines = [t.text for t in self._session.transcript if t.speaker == "caller"]
        return " / ".join(lines) or "(the caller said nothing)"

    async def _close(self, reason: str, phrase_keys: list[str] | None = None) -> None:
        """Say the closing phrases, then end the pipeline once they have been played."""
        if self.mode is Mode.CLOSING:
            return
        self.mode = Mode.CLOSING
        self.end_reason = reason
        self._disarm_filler()
        await self._cancel(self._turn_task)
        await self._cancel(self._hold_task)
        await self._close_response()
        for key in phrase_keys or []:
            await self._speak(phrase(key), record=True)
        await self._persist()
        # Downstream, behind the goodbye: the worker ends after everything ahead is flushed.
        await self.push_frame(EndWorkerFrame(reason=reason))

    async def _cancel(self, task: asyncio.Task[None] | None) -> None:
        if task is not None and task is not asyncio.current_task() and not task.done():
            await self.cancel_task(task)


class HoldListener(FrameProcessor):
    """Sits between STT and the user aggregator. While the caller is on hold, transcripts go to
    the hold manager (buffered, canned reply) instead of becoming model turns; the aggregator
    would drop them anyway while the caller is muted."""

    def __init__(self, agent: AgentBrainService, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._agent = agent

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if self._agent.on_hold and isinstance(
            frame, TranscriptionFrame | InterimTranscriptionFrame
        ):
            if isinstance(frame, TranscriptionFrame):
                self._agent.on_hold_utterance(frame.text)
            return
        await self.push_frame(frame, direction)


def message_text(message: Any) -> str:
    """Plain text of a Pipecat context message (string or list of content parts)."""
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
        ]
        return " ".join(p.strip() for p in parts if p.strip())
    return ""
