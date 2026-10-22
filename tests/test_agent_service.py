"""AgentBrainService inside a real (test) Pipecat pipeline, with a scripted brain."""

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

from pipecat.frames.frames import (
    AggregatedTextFrame,
    EndWorkerFrame,
    Frame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    OutputAudioRawFrame,
    TTSSpeakFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.tests.utils import SleepFrame, run_test

from app.agent.brain import FALLBACK_PHRASE_LT
from app.config import Settings
from app.models import CallSession
from app.telephony.agent_service import WRAP_UP_EVENT, AgentBrainService, CallLimits, Mode, phrase

FAIL = object()
END = object()
Step = str | float | object | Callable[[], Any]


class ScriptedBrain:
    """Stands in for AgentBrain: each respond() plays the next script."""

    def __init__(self, *scripts: list[Step]) -> None:
        self._scripts = list(scripts)
        self.inputs: list[tuple[str, str | None]] = []
        self.opening: str | None = None
        self.ended = False
        self.last_turn_failed = False
        self.closed_early = False

    def add_opening(self, text: str) -> None:
        self.opening = text

    async def respond(self, text: str, event: str | None = None) -> AsyncIterator[str]:
        self.inputs.append((text, event))
        self.last_turn_failed = False
        try:
            for step in self._scripts.pop(0):
                if isinstance(step, float):
                    await asyncio.sleep(step)
                elif step is FAIL:
                    self.last_turn_failed = True
                    yield FALLBACK_PHRASE_LT
                elif step is END:
                    self.ended = True
                elif callable(step):
                    await step()
                else:
                    yield str(step)
        except (asyncio.CancelledError, GeneratorExit):
            self.closed_early = True
            raise


def make_agent(brain: ScriptedBrain, **limits: Any) -> tuple[AgentBrainService, CallSession]:
    session = CallSession(call_sid="CA1", caller_number="+37060000000")
    defaults: dict[str, Any] = {"filler_after_seconds": 5, "caller_idle_seconds": 60}
    settings = Settings(_env_file=None, **{**defaults, **limits})

    async def persist() -> None:
        return None

    agent = AgentBrainService(
        session=session,
        limits=CallLimits(settings),
        hold_phrase_audio=lambda: b"\x00\x00" * 800,
        persist=persist,
    )
    agent.attach(brain)  # type: ignore[arg-type]
    return agent, session


def caller(*texts: str) -> LLMContextFrame:
    context = LLMContext()
    for text in texts:
        context.add_message({"role": "user", "content": text})
    return LLMContextFrame(context=context)


async def run(agent: AgentBrainService, *frames: Frame) -> list[Frame]:
    down, _ = await run_test(agent, frames_to_send=list(frames), expected_down_frames=None)
    return list(down)


def spoken(frames: list[Frame]) -> list[str]:
    return [f.text for f in frames if isinstance(f, TTSSpeakFrame | AggregatedTextFrame)]


async def test_greeting_then_reply_as_one_response() -> None:
    brain = ScriptedBrain(["Labas.", "Kuo galiu padėti?"])
    agent, _ = make_agent(brain)
    frames = await run(agent, caller("Sveiki"), SleepFrame(sleep=0.2))

    assert brain.opening == phrase("greeting")
    assert spoken(frames) == [phrase("greeting"), "Labas.", "Kuo galiu padėti?"]
    kinds = [type(f) for f in frames if not isinstance(f, TTSSpeakFrame)]
    assert kinds == [
        LLMFullResponseStartFrame,
        AggregatedTextFrame,
        AggregatedTextFrame,
        LLMFullResponseEndFrame,
    ]
    assert brain.inputs == [("Sveiki", None)]


async def test_only_new_caller_text_is_answered_and_speculation_ignored() -> None:
    brain = ScriptedBrain(["Pirmas."], ["Antras."])
    agent, _ = make_agent(brain)
    speculative = caller("Sveiki", "Spėjimas")
    speculative.speculation = True
    await run(
        agent,
        caller("Sveiki"),
        SleepFrame(sleep=0.1),
        speculative,
        SleepFrame(sleep=0.1),
        caller("Sveiki", "Kitas"),
        SleepFrame(sleep=0.1),
    )
    assert [text for text, _ in brain.inputs] == ["Sveiki", "Kitas"]


async def test_barge_in_cancels_the_turn() -> None:
    brain = ScriptedBrain(["Pirmas sakinys.", 1.0, "Niekada neištarta."])
    agent, _ = make_agent(brain)
    frames = await run(
        agent, caller("Sveiki"), SleepFrame(sleep=0.1), InterruptionFrame(), SleepFrame(sleep=0.2)
    )
    assert "Niekada neištarta." not in spoken(frames)
    assert brain.closed_early


async def test_filler_when_the_model_is_slow() -> None:
    brain = ScriptedBrain([0.25, "Atsakymas."])
    agent, _ = make_agent(brain, filler_after_seconds=0.1)
    frames = await run(agent, caller("Sveiki"), SleepFrame(sleep=0.5))
    said = spoken(frames)
    assert said[1:] == [phrase("filler"), phrase("filler_long"), "Atsakymas."]


async def test_stuck_model_counts_as_failure_after_fillers() -> None:
    brain = ScriptedBrain([5.0, "Per vėlu."])
    agent, _ = make_agent(brain, filler_after_seconds=0.05)
    frames = await run(agent, caller("Sveiki"), SleepFrame(sleep=0.4))
    said = spoken(frames)
    assert said[-1] == phrase("technical_issue")
    assert "Per vėlu." not in said
    assert agent.mode is Mode.AGENT  # one failure: the next turn retries the model


async def test_two_llm_failures_switch_to_message_taking() -> None:
    brain = ScriptedBrain([FAIL], [FAIL])
    agent, session = make_agent(brain)
    frames = await run(
        agent,
        caller("Sveiki"),
        SleepFrame(sleep=0.1),
        caller("Sveiki", "Aš kurjeris"),
        SleepFrame(sleep=0.1),
        caller("Sveiki", "Aš kurjeris", "Paliksiu prie durų"),
        SleepFrame(sleep=0.1),
    )
    assert agent.mode is Mode.CLOSING
    assert spoken(frames)[-2:] == [phrase("message_taken"), phrase("goodbye")]
    assert any(isinstance(f, EndWorkerFrame) for f in frames)
    assert len(brain.inputs) == 2  # the message itself never went to the model
    purpose, details = agent.owner_notes[0]
    assert "Paliksiu prie durų" in details
    assert session.transcript[-1].text == phrase("goodbye")


async def test_end_call_says_goodbye_then_ends_pipeline() -> None:
    brain = ScriptedBrain(["Ačiū, viso gero!", END])
    agent, _ = make_agent(brain)

    def end() -> None:
        agent.request_end("done")

    brain._scripts[0].insert(1, lambda: asyncio.sleep(0, end()))  # type: ignore[func-returns-value]
    frames = await run(agent, caller("Viskas"), SleepFrame(sleep=0.2))
    end_index = next(i for i, f in enumerate(frames) if isinstance(f, EndWorkerFrame))
    goodbye_index = next(
        i for i, f in enumerate(frames) if getattr(f, "text", "") == "Ačiū, viso gero!"
    )
    assert goodbye_index < end_index
    assert agent.mode is Mode.CLOSING and agent.end_reason == "done"
    assert agent.user_muted


async def test_hold_plays_music_mutes_caller_and_ignores_barge_in() -> None:
    agent_ref: list[AgentBrainService] = []
    muted_during_hold: list[bool] = []

    async def hold() -> None:
        async with agent_ref[0].hold():
            muted_during_hold.append(agent_ref[0].user_muted)
            await asyncio.sleep(0.3)

    brain = ScriptedBrain(["Prašau palaukti.", hold, "Savininkas sako: palikite prie durų."])
    agent, _ = make_agent(brain)
    agent_ref.append(agent)
    frames = await run(
        agent,
        caller("Siuntinys"),
        SleepFrame(sleep=0.15),
        InterruptionFrame(),
        SleepFrame(sleep=0.4),
    )
    assert muted_during_hold == [True]
    assert not agent.user_muted
    assert any(isinstance(f, OutputAudioRawFrame) for f in frames)
    assert spoken(frames)[-1] == "Savininkas sako: palikite prie durų."
    # "please hold" was closed as its own response before the music started.
    first_end = next(i for i, f in enumerate(frames) if isinstance(f, LLMFullResponseEndFrame))
    first_music = next(i for i, f in enumerate(frames) if isinstance(f, OutputAudioRawFrame))
    assert first_end < first_music


async def test_wrap_up_event_near_the_turn_limit_then_hard_cap() -> None:
    brain = ScriptedBrain(["Vienas."], ["Du."])
    agent, _ = make_agent(brain, max_caller_turns=2)
    frames = await run(
        agent,
        caller("a"),
        SleepFrame(sleep=0.1),
        caller("a", "b"),
        SleepFrame(sleep=0.1),
        caller("a", "b", "c"),
        SleepFrame(sleep=0.1),
    )
    assert brain.inputs == [("a", WRAP_UP_EVENT), ("b", WRAP_UP_EVENT)]
    assert spoken(frames)[-2:] == [phrase("time_limit"), phrase("goodbye")]
    assert agent.owner_notes and "turn limit" in agent.owner_notes[0][0]


async def test_call_length_cap() -> None:
    brain = ScriptedBrain()
    agent, _ = make_agent(brain, max_call_seconds=0.3, wrap_up_seconds=0)
    frames = await run(agent, SleepFrame(sleep=1.0))
    assert spoken(frames)[-2:] == [phrase("time_limit"), phrase("goodbye")]
    assert any(isinstance(f, EndWorkerFrame) for f in frames)


async def test_silent_caller_is_prompted_then_call_ends() -> None:
    brain = ScriptedBrain()
    agent, _ = make_agent(brain, caller_idle_seconds=0.2)
    frames = await run(agent, SleepFrame(sleep=2.2))  # the monitor checks every 0.5 s
    said = spoken(frames)
    assert said.count(phrase("are_you_there")) == 2
    assert said[-1] == phrase("goodbye")
    assert agent.end_reason == "caller silent"


async def test_tts_failure_switches_to_cached_message_taking() -> None:
    brain = ScriptedBrain(["Atsakymas.", 1.0, "Daugiau."])
    agent, _ = make_agent(brain)

    async def fail_tts() -> None:
        await agent.on_tts_failure()

    async def scenario() -> list[Frame]:
        task = asyncio.create_task(run(agent, caller("Sveiki"), SleepFrame(sleep=0.4)))
        await asyncio.sleep(0.15)
        await fail_tts()
        return await task

    frames = await scenario()
    assert agent.mode is Mode.MESSAGE
    assert spoken(frames)[-1] == phrase("technical_issue")
    assert brain.closed_early


async def test_stt_failure_ends_call_and_alerts_owner() -> None:
    agent, _ = make_agent(ScriptedBrain())

    async def scenario() -> list[Frame]:
        task = asyncio.create_task(run(agent, SleepFrame(sleep=0.3)))
        await asyncio.sleep(0.1)
        await agent.on_stt_failure()
        return await task

    frames = await scenario()
    assert spoken(frames)[-2:] == [phrase("cant_hear"), phrase("goodbye")]
    assert agent.owner_alerts
    assert any(isinstance(f, EndWorkerFrame) for f in frames)
