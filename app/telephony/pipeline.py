"""Builds and runs the Pipecat pipeline for one Twilio media stream.

transport.input -> Silero VAD -> STT -> user aggregator -> AgentBrainService -> TTS
-> transport.output -> assistant aggregator
"""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import WebSocket
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import Frame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.runner.types import CallData
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat.turns.user_mute import (
    BaseUserMuteStrategy,
    MuteUntilFirstBotCompleteUserMuteStrategy,
)
from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from app.agent.brain import create_brain
from app.config import Settings
from app.models import CallSession, CallStatus
from app.owner_channel import OwnerChannel
from app.rules import RulesEngine
from app.speech.base import TELEPHONY_SAMPLE_RATE, STTConfig, VoiceConfig
from app.speech.factory import create_stt, create_tts
from app.speech.phrases_lt import PHRASES_BY_KEY
from app.telephony.agent_service import AgentBrainService, CallLimits
from app.telephony.call_handlers import CallOwnerBridge, CallRecorder
from app.telephony.latency import TurnLatencyObserver
from app.telephony.phrase_cache import PhraseCache
from app.telephony.speech_services import ProviderSTTService, ProviderTTSService

logger = logging.getLogger(__name__)

# Seconds without any pipeline activity before Pipecat cancels the call (last-resort guard;
# the agent's own caller-idle handling ends silent calls much sooner).
_PIPELINE_IDLE_TIMEOUT = 90


@dataclass
class TelephonyDeps:
    """Process-wide dependencies of the call pipeline, built once in the app lifespan."""

    settings: Settings
    rules: RulesEngine
    owner_channel: OwnerChannel | None
    phrases: PhraseCache
    # None when STT/TTS can be built; otherwise why not (missing keys or SDKs).
    speech_problem: str | None = None
    # End-of-call bookkeeping still running (kept referenced, awaited on shutdown).
    _background: set[asyncio.Task[None]] = field(default_factory=set)

    def spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def drain(self, timeout: float) -> None:
        if self._background:
            await asyncio.wait(self._background, timeout=timeout)


class PredicateUserMuteStrategy(BaseUserMuteStrategy):
    """Mutes the caller while `is_muted()` is true (on hold, saying goodbye)."""

    def __init__(self, is_muted: Callable[[], bool]) -> None:
        super().__init__()
        self._is_muted = is_muted

    async def process_frame(self, frame: Frame) -> bool:
        await super().process_frame(frame)
        return self._is_muted()


def build_serializer(settings: Settings, call_data: CallData) -> TwilioFrameSerializer:
    token = settings.twilio_auth_token.get_secret_value() if settings.twilio_auth_token else None
    # With credentials, Pipecat hangs up through the REST API when the pipeline ends. Without,
    # closing the stream still ends the call (the TwiML has nothing after <Connect>).
    hang_up = bool(settings.twilio_account_sid and token)
    return TwilioFrameSerializer(
        stream_sid=call_data.stream_id or "",
        call_sid=call_data.call_id,
        account_sid=settings.twilio_account_sid,
        auth_token=token,
        params=TwilioFrameSerializer.InputParams(auto_hang_up=hang_up),
    )


async def run_call(
    websocket: WebSocket, call_data: CallData, session: CallSession, deps: TelephonyDeps
) -> None:
    """Run one call until it ends; stores the transcript and notifies the owner afterwards."""
    settings = deps.settings
    recorder = CallRecorder(settings.db_path, session)
    agent = AgentBrainService(
        session=session,
        limits=CallLimits(settings),
        hold_phrase_audio=lambda: deps.phrases.get(PHRASES_BY_KEY["still_trying"].text),
        persist=recorder.flush,
    )
    bridge = CallOwnerBridge(
        session, agent, deps.owner_channel, settings.db_path, settings.hold_timeout_seconds
    )
    agent.attach(
        create_brain(settings, bridge.tool_handlers(), deps.rules, session.caller_number, session)
    )

    stt = ProviderSTTService(
        create_stt(settings),
        STTConfig(languages=settings.stt_language_list),
        on_failure=agent.on_stt_failure,
        ttfs_p99_latency=settings.stt_final_p99_seconds,
    )
    tts = ProviderTTSService(
        create_tts(settings),
        VoiceConfig(language=settings.caller_language),
        deps.phrases,
        on_failure=agent.on_tts_failure,
        first_byte_timeout=settings.tts_first_byte_timeout_seconds,
        chunk_timeout=settings.speech_timeout_seconds,
    )
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=build_serializer(settings, call_data),
        ),
    )
    vad = VADProcessor(
        vad_analyzer=SileroVADAnalyzer(
            sample_rate=TELEPHONY_SAMPLE_RATE, params=VADParams(stop_secs=settings.vad_stop_secs)
        )
    )
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        LLMContext(),
        user_params=LLMUserAggregatorParams(
            user_turn_strategies=UserTurnStrategies(
                stop=[
                    SpeechTimeoutUserTurnStopStrategy(
                        user_speech_timeout=settings.turn_speech_timeout
                    )
                ]
            ),
            user_mute_strategies=[
                # The AI + recording disclosure is always heard in full.
                MuteUntilFirstBotCompleteUserMuteStrategy(),
                PredicateUserMuteStrategy(lambda: agent.user_muted),
            ],
            # Noise that interrupts the agent without words is not a turn to answer.
            empty_user_turn=None,
        ),
    )
    pipeline = Pipeline(
        [
            transport.input(),
            vad,
            stt,
            user_aggregator,
            agent,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )
    latency = TurnLatencyObserver(session.call_sid, settings.latency_target_ms)
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_sample_rate=TELEPHONY_SAMPLE_RATE,
            enable_metrics=True,
        ),
        observers=[latency],
        idle_timeout_secs=_PIPELINE_IDLE_TIMEOUT,
        enable_rtvi=False,
    )

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport: object, client: object) -> None:
        logger.info("Caller hung up", extra={"call_sid": session.call_sid})
        await worker.cancel(reason="caller hung up")

    session.status = CallStatus.IN_PROGRESS
    await recorder.flush()
    try:
        await WorkerRunner(handle_sigint=False).run(worker)
    except Exception:
        session.status = CallStatus.FAILED
        logger.exception("Call pipeline crashed", extra={"call_sid": session.call_sid})
        raise
    finally:
        if session.status is not CallStatus.FAILED:
            session.status = CallStatus.COMPLETED
        session.ended_at = datetime.now(UTC)
        # The WebSocket endpoint can be cancelled as the socket closes, which would cut any
        # await here short; the bookkeeping runs in a task of its own instead.
        deps.spawn(finish_call(session, recorder, agent, bridge, latency))


async def finish_call(
    session: CallSession,
    recorder: CallRecorder,
    agent: AgentBrainService,
    bridge: CallOwnerBridge,
    latency: TurnLatencyObserver,
) -> None:
    await recorder.flush()
    for purpose, details in agent.owner_notes:
        await bridge.save_note(purpose, details)
    for alert in agent.owner_alerts:
        await bridge.alert(alert)
    summary = latency.tracker.summary()
    logger.info(
        "Call finished",
        extra={"call_sid": session.call_sid, "end_reason": agent.end_reason, **summary},
    )
    await bridge.send_summary(agent.end_reason or "arayan kapattı", summary)
