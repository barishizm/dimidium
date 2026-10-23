"""Call lifecycle of a GPT-Live SIP call against a fake sideband connection."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from app import db
from app.config import Settings
from app.live.call import (
    DISCLOSURE_INSTRUCTION,
    FALLBACK_INSTRUCTION,
    HOLD_COMMENTARY,
    LIMIT_OUTPUT,
    MESSAGE_ONLY_OUTPUT,
    WRAP_UP_INSTRUCTION,
    LiveCall,
    LiveCallTimings,
)
from app.models import CallSession, CallStatus
from tests.live_fakes import (
    FAST,
    GREETING_LT,
    FakeChannel,
    FakeConnector,
    FakeControl,
    FakeHost,
    FakeLink,
    agent_says,
    caller_says,
    closed,
    ev,
    function_call,
    option_decision,
)

ASK_ARGS = json.dumps(
    {
        "summary_tr": "DPD kuryesi, paket için kapıda bekliyor.",
        "caller_said_lt": "Turiu siuntinį, ar galiu palikti prie durų?",
        "options": [
            {"id": "door", "label_tr": "Kapıya bırak", "meaning_en": "Leave it at the door."},
            {"id": "later", "label_tr": "Sonra gel", "meaning_en": "Come back tomorrow."},
        ],
    },
    ensure_ascii=False,
)


_HARNESSES: list["Harness"] = []


@pytest.fixture(autouse=True)
async def _cancel_leftover_calls() -> AsyncIterator[None]:
    yield
    while _HARNESSES:
        await _HARNESSES.pop().cleanup()


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        *links: FakeLink | Exception,
        channel: FakeChannel | None = None,
        timings: LiveCallTimings = FAST,
        message_only: bool = False,
        **settings: Any,
    ) -> None:
        self.settings = Settings(  # type: ignore[call-arg]
            owner_name="Test Owner", db_path=str(tmp_path / "calls.db"), **settings
        )
        self.links = list(links) or [FakeLink()]
        self.connector = FakeConnector(*self.links)
        self.control = FakeControl(self.connector)
        self.host = FakeHost()
        self.channel = channel if channel is not None else FakeChannel()
        self.session = CallSession(
            call_sid="live-test0001",
            caller_number="+37060000001",
            live_session_id="live_sess_1",
            message_only=message_only,
        )
        self.call = LiveCall(
            self.session,
            settings=self.settings,
            connector=self.connector,
            control=self.control,
            host=self.host,
            owner_channel=self.channel,  # type: ignore[arg-type]
            timings=timings,
        )
        self.task: asyncio.Task[None] | None = None
        _HARNESSES.append(self)

    async def cleanup(self) -> None:
        """Cancel whatever a failed test left running, so it fails instead of hanging."""
        tasks = [t for t in [self.task, *self.host.tasks] if t is not None and not t.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @property
    def link(self) -> FakeLink:
        link = self.links[0]
        assert isinstance(link, FakeLink)
        return link

    async def start(self) -> None:
        await db.init_db(self.settings.db_path)
        self.task = asyncio.create_task(self.call.run())
        await self.connector.wait_attached()

    async def finish(self, timeout: float = 3.0) -> None:
        """Wait for the call to end and for the post-call work."""
        assert self.task is not None
        await asyncio.wait_for(self.task, timeout)
        await asyncio.wait_for(self.host.drain(), timeout)

    def sent(self, kind: str, link: FakeLink | None = None) -> list[dict[str, Any]]:
        return [e for e in (link or self.link).sent if e["type"] == kind]

    def instructions(self) -> list[str]:
        return [e["content"] for e in self.sent("session.instructions.append")]

    def outputs(self) -> list[str]:
        return [e["item"]["output"] for e in self.sent("response.item.create")]

    async def stored_turns(self) -> list[tuple[str, str]]:
        async with aiosqlite.connect(self.settings.db_path) as conn:
            cursor = await conn.execute(
                "SELECT speaker, text FROM transcript_turns WHERE call_sid = ? ORDER BY id",
                (self.session.call_sid,),
            )
            return [(row[0], row[1]) for row in await cursor.fetchall()]


async def _delivered_messages(db_path: str, call_sid: str) -> list[str]:
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            "SELECT message FROM messages_for_owner WHERE call_sid = ? AND delivered = 1",
            (call_sid,),
        )
        return [row[0] for row in await cursor.fetchall()]


async def until(condition: Any, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        assert loop.time() < deadline, "condition not reached in time"
        await asyncio.sleep(0.005)


# -- transcripts, disclosure, session end ----------------------------------------------------


async def test_transcripts_are_stored_and_the_call_is_reported(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await h.start()
    h.link.push(
        agent_says("Laba diena! Atsiliepia dirbtinio intelekto asistentas. "),
        agent_says("Pokalbis įrašomas ir transkribuojamas."),
        caller_says("Čia DPD kurjeris, "),
        caller_says("turiu siuntinį."),
        agent_says("Supratau, minutėlę."),
        closed("remote_hangup"),
    )
    await h.finish()

    assert [(t.speaker, t.text) for t in h.session.transcript] == [
        (
            "agent",
            "Laba diena! Atsiliepia dirbtinio intelekto asistentas. "
            "Pokalbis įrašomas ir transkribuojamas.",
        ),
        ("caller", "Čia DPD kurjeris, turiu siuntinį."),
        ("agent", "Supratau, minutėlę."),
    ]
    assert await h.stored_turns() == [(t.speaker, t.text) for t in h.session.transcript]
    assert h.call.disclosure_verified
    assert DISCLOSURE_INSTRUCTION not in h.instructions()
    assert h.call.close_reason == "remote_hangup"
    assert h.call.usage_seconds == 42
    assert h.session.status is CallStatus.COMPLETED
    assert h.session.ended_at is not None
    assert h.control.hangups == []
    report = h.channel.info[-1]
    assert "Arama bitti" in report and "arayan kapattı" in report
    assert h.host.tracked == {}


async def test_replayed_events_after_reattach_are_not_counted_twice(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await h.start()
    delta = caller_says("Labas.")
    h.link.push(delta, dict(delta), closed())
    await h.finish()

    assert [t.text for t in h.session.transcript] == ["Labas."]


async def test_missing_disclosure_is_sent_as_instruction_and_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    h = Harness(tmp_path)
    caplog.set_level(logging.WARNING)
    await h.start()
    h.link.push(agent_says("Laba diena, kuo galiu padėti?"))

    await until(lambda: DISCLOSURE_INSTRUCTION in h.instructions())
    instruction = h.sent("session.instructions.append")[0]
    assert instruction["delegation_id"] is None
    assert "disclosure not found" in caplog.text
    assert h.call.disclosure_resent and not h.call.disclosure_verified

    h.link.push(
        agent_says("Noriu informuoti, kad su jumis kalba dirbtinio intelekto asistentas, "),
        agent_says("o pokalbis įrašomas ir transkribuojamas."),
    )
    await until(lambda: h.call.disclosure_verified)
    h.link.push(closed())
    await h.finish()
    assert not any("doğrulanamadı" in text for text in h.channel.info)


async def test_disclosure_never_spoken_is_reported_to_the_owner(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await h.start()
    h.link.push(agent_says("Labas."))
    await until(lambda: h.call.disclosure_resent)
    await asyncio.sleep(FAST.disclosure_deadline + 0.1)
    h.link.push(closed())
    await h.finish()

    assert any("doğrulanamadı" in text for text in h.channel.info)


async def test_disclosure_check_accepts_english(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await h.start()
    h.link.push(
        agent_says("Hello, you are talking to an AI assistant. "),
        agent_says("This call is recorded and transcribed."),
        closed(),
    )
    await h.finish()
    assert h.call.disclosure_verified


# -- functions ---------------------------------------------------------------------------------


async def test_ask_owner_returns_the_decision_and_continues_the_backend(tmp_path: Path) -> None:
    channel = FakeChannel(option_decision("door"), delay=0.15)
    h = Harness(tmp_path, channel=channel)
    await h.start()
    h.link.push(agent_says(GREETING_LT), function_call("ask_owner", ASK_ARGS))
    await until(lambda: channel.asked)
    h.link.push(caller_says("Ar dar ilgai?"))

    await until(lambda: h.outputs())
    assert h.outputs() == [
        "OWNER_DECISION: Leave it at the door.\nCALLER_SAID_DURING_HOLD: Ar dar ilgai?"
    ]
    item = h.sent("response.item.create")[0]["item"]
    assert item["type"] == "function_call_output" and item["call_id"] == "call_1"
    # The continuation follows the output.
    kinds = [e["type"] for e in h.link.sent]
    assert kinds.index("response.create") > kinds.index("response.item.create")
    assert h.session.owner_decision == "Leave it at the door."
    assert channel.asked[0]["caller_number"] == "+37060000001"
    # The agent said nothing on hold, so the app announced it.
    assert HOLD_COMMENTARY in [e["content"] for e in h.sent("session.commentary.append")]

    h.link.push(closed())
    await h.finish()
    assert h.session.status is CallStatus.COMPLETED


async def test_same_function_call_id_runs_once(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await h.start()
    args = json.dumps({"reason": "spam"})
    h.link.push(
        function_call("flag_suspicious", args, "call_x"),
        function_call("flag_suspicious", args, "call_x"),
    )
    await until(lambda: h.outputs())
    await asyncio.sleep(0.05)
    assert h.outputs() == ["OK"]
    h.link.push(closed())
    await h.finish()


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        ("ask_owner", '{"summary_tr": "x"}', "ERROR: invalid arguments"),
        ("ask_owner", "not json", "ERROR: invalid arguments"),
        ("end_call", '{"reason": "done", "admin": true}', "ERROR: invalid arguments"),
        ("transfer_call", "{}", "ERROR: unknown function"),
    ],
)
async def test_invalid_function_calls_are_refused_without_side_effects(
    tmp_path: Path, name: str, arguments: str, expected: str
) -> None:
    h = Harness(tmp_path)
    await h.start()
    h.link.push(function_call(name, arguments))
    await until(lambda: h.outputs())

    assert h.outputs()[0].startswith(expected)
    assert h.channel.asked == []
    assert h.control.hangups == []
    h.link.push(closed())
    await h.finish()


async def test_ask_owner_is_capped_and_the_call_wrapped_up(tmp_path: Path) -> None:
    channel = FakeChannel(option_decision("door"))
    h = Harness(tmp_path, channel=channel, max_owner_questions=2)
    await h.start()
    for n in range(3):
        h.link.push(function_call("ask_owner", ASK_ARGS, f"call_{n}"))
        await until(lambda n=n: len(h.outputs()) == n + 1)

    assert len(channel.asked) == 2
    assert h.outputs()[2] == LIMIT_OUTPUT
    assert WRAP_UP_INSTRUCTION in h.instructions()
    await until(lambda: h.control.hangups)
    await h.finish()
    assert h.call.end_reason() == "soru sınırı aşıldı"


async def test_voicemail_call_never_asks_the_owner(tmp_path: Path) -> None:
    channel = FakeChannel(option_decision())
    h = Harness(tmp_path, channel=channel, message_only=True)
    await h.start()
    h.link.push(function_call("ask_owner", ASK_ARGS))
    await until(lambda: h.outputs())
    assert h.outputs() == [MESSAGE_ONLY_OUTPUT]
    assert channel.asked == []
    h.link.push(closed())
    await h.finish()


async def test_owner_timeout_tells_the_agent_to_take_a_message(tmp_path: Path) -> None:
    from app.owner_channel import DecisionKind, OwnerDecision

    channel = FakeChannel(OwnerDecision(DecisionKind.TIMEOUT))
    h = Harness(tmp_path, channel=channel, hold_timeout_seconds=60)
    await h.start()
    h.link.push(function_call("ask_owner", ASK_ARGS))
    await until(lambda: h.outputs())
    assert h.outputs()[0].startswith("OWNER_TIMEOUT: the owner did not answer within 60 seconds")
    h.link.push(closed())
    await h.finish()


async def test_save_message_is_stored_and_pushed(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await h.start()
    args = {"purpose": "Kurye aradı", "caller_name": "Jonas", "callback_number": "+3706"}
    h.link.push(function_call("save_message", json.dumps(args)))
    await until(lambda: h.outputs())
    assert h.outputs() == ["OK"]
    h.link.push(closed())
    await h.finish()

    assert any(text.startswith("📝 Mesaj") and "Jonas" in text for text in h.channel.info)
    stored = await _delivered_messages(h.settings.db_path, h.session.call_sid)
    assert stored and "Kurye aradı" in stored[0]


async def test_caller_hanging_up_on_hold_cancels_the_question(tmp_path: Path) -> None:
    channel = FakeChannel(decision=None)
    h = Harness(tmp_path, channel=channel)
    await h.start()
    h.link.push(function_call("ask_owner", ASK_ARGS))
    await until(lambda: channel.asked)
    assert h.session.status is CallStatus.ON_HOLD
    h.link.push(closed("remote_hangup"))
    await h.finish()

    assert channel.cancelled == 1
    assert h.call.hung_up_on_hold
    assert any(text.startswith("📵 Arayan beklerken kapattı") for text in channel.info)


# -- ending the call ---------------------------------------------------------------------------


async def test_end_call_waits_for_the_goodbye_before_hanging_up(tmp_path: Path) -> None:
    timings = LiveCallTimings(**{**FAST.__dict__, "goodbye_quiet": 0.15})
    h = Harness(tmp_path, timings=timings)
    await h.start()
    h.link.push(function_call("end_call", json.dumps({"reason": "done"})))
    await until(lambda: h.outputs() == ["OK"])

    # The goodbye is being spoken: no hangup while transcript deltas keep coming.
    for word in ("Ačiū ", "už ", "skambutį. ", "Viso ", "gero!"):
        h.link.push(agent_says(word))
        await asyncio.sleep(0.05)
        assert h.control.hangups == []
    await until(lambda: h.control.hangups, timeout=1.0)
    await h.finish()

    assert h.control.hangups == ["live_sess_1"]
    assert h.session.transcript[-1].text == "Ačiū už skambutį. Viso gero!"
    assert h.call.end_reason() == "asistan bitirdi: done"


async def test_hangup_waits_until_the_timeline_passes_the_last_audio(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await h.start()
    h.link.push(function_call("end_call", json.dumps({"reason": "done"})))
    await until(lambda: h.outputs() == ["OK"])
    # Timeline at 1000 ms now; the goodbye audio was generated up to 1400 ms.
    h.link.push(
        ev("session.instructions.appended", start_ms=1000, end_ms=1000),
        ev("session.output_audio.delta", delta="", start_ms=1000, end_ms=1400),
    )
    loop = asyncio.get_running_loop()
    start = loop.time()
    await until(lambda: h.control.hangups, timeout=2.0)
    # 400 ms of audio still playing + 300 ms margin.
    assert loop.time() - start >= 0.6
    await h.finish()


async def test_goodbye_wait_is_capped(tmp_path: Path) -> None:
    timings = LiveCallTimings(**{**FAST.__dict__, "goodbye_max": 0.3})
    h = Harness(tmp_path, timings=timings)
    await h.start()
    h.link.push(function_call("end_call", json.dumps({"reason": "done"})))

    async def keep_talking() -> None:
        while not h.control.hangups:
            h.link.push(agent_says("bla "))
            await asyncio.sleep(0.01)

    talker = asyncio.create_task(keep_talking())
    await until(lambda: h.control.hangups, timeout=1.0)
    await talker
    await h.finish()


async def test_max_duration_wraps_up_then_hangs_up(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_call_seconds=0.3, wrap_up_seconds=0.15)
    await h.start()
    await until(lambda: len(h.instructions()) >= 1)
    assert "almost at its time limit" in h.instructions()[0]
    await until(lambda: WRAP_UP_INSTRUCTION in h.instructions())
    await until(lambda: h.control.hangups)
    await h.finish()
    assert h.call.end_reason() == "süre sınırı"


async def test_hangup_without_session_closed_still_finishes(tmp_path: Path) -> None:
    link = FakeLink()
    h = Harness(tmp_path, link)
    h.control._connector = None  # OpenAI never confirms with session.closed
    await h.start()
    h.link.push(function_call("end_call", json.dumps({"reason": "done"})))
    await h.finish()
    assert h.control.hangups == ["live_sess_1"]


# -- failures ------------------------------------------------------------------------------------


async def test_sideband_drop_reattaches_once_and_continues(tmp_path: Path) -> None:
    first, second = FakeLink(), FakeLink()
    h = Harness(tmp_path, first, second)
    await h.start()
    first.push(caller_says("Labas, "))
    first.drop()
    await h.connector.wait_attached(2)
    second.push(caller_says("čia Jonas."), closed())
    await h.finish()

    assert h.connector.attached == ["live_sess_1", "live_sess_1"]
    assert [t.text for t in h.session.transcript] == ["Labas, čia Jonas."]
    assert h.control.hangups == []
    assert h.call.failure is None


async def test_function_output_waits_for_the_reattached_sideband(tmp_path: Path) -> None:
    first, second = FakeLink(), FakeLink()
    channel = FakeChannel(option_decision("door"), delay=0.1)
    h = Harness(tmp_path, first, second, channel=channel)
    await h.start()
    first.push(function_call("ask_owner", ASK_ARGS))
    await until(lambda: channel.asked)
    first.drop()
    await h.connector.wait_attached(2)

    await until(lambda: h.sent("response.item.create", second))
    assert h.sent("response.item.create", second)[0]["item"]["output"].startswith("OWNER_DECISION")
    second.push(closed())
    await h.finish()


async def test_sideband_lost_for_good_hangs_up_saves_and_notifies(tmp_path: Path) -> None:
    first = FakeLink()
    h = Harness(tmp_path, first, ConnectionError("attach refused"))
    await h.start()
    first.push(caller_says("Čia Jonas iš DPD."))
    first.drop()
    await h.finish()

    assert h.control.hangups == ["live_sess_1"]
    assert h.call.failure == "sideband lost"
    assert any("Bağlantı sorunu" in text for text in h.channel.info)
    stored = await _delivered_messages(h.settings.db_path, h.session.call_sid)
    assert stored and "Čia Jonas iš DPD." in stored[0]
    assert h.call.end_reason() == "bağlantı koptu"


async def test_backend_failure_is_retried_once(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await h.start()
    h.link.push(ev("response.event", event={"type": "response.failed", "response": {}}))
    await until(lambda: h.sent("response.create"))
    assert h.call.failure is None
    assert FALLBACK_INSTRUCTION not in h.instructions()
    h.link.push(closed())
    await h.finish()


async def test_repeated_backend_failure_falls_back_to_a_message(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await h.start()
    h.link.push(caller_says("Čia Jonas, dėl siuntinio."))
    for _ in range(2):
        h.link.push(ev("response.event", event={"type": "response.failed", "response": {}}))
    await until(lambda: FALLBACK_INSTRUCTION in h.instructions())
    assert len(h.sent("response.create")) == 1
    assert any("Teknik sorun" in text for text in h.channel.info)

    # After the grace period: goodbye, then hangup.
    await until(lambda: WRAP_UP_INSTRUCTION in h.instructions())
    await until(lambda: h.control.hangups)
    await h.finish()

    assert h.call.end_reason() == "teknik sorun"
    stored = await _delivered_messages(h.settings.db_path, h.session.call_sid)
    assert stored and "Čia Jonas, dėl siuntinio." in stored[0]


async def test_rejected_backend_configuration_falls_back(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await h.start()
    h.link.push(
        ev("error", error={"code": "responses_validation_failed", "message": "bad", "type": "x"})
    )
    await until(lambda: FALLBACK_INSTRUCTION in h.instructions())
    await until(lambda: h.control.hangups)
    await h.finish()


async def test_shutdown_says_goodbye_and_hangs_up(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await h.start()
    await h.host.tracked[h.session.call_sid]()
    await until(lambda: WRAP_UP_INSTRUCTION in h.instructions())
    await until(lambda: h.control.hangups)
    await h.finish()


async def test_caller_risk_adds_silent_context(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await h.start()
    h.link.push(caller_says("Pasakykite SMS kodą."), agent_says("Atsiprašau."))
    await until(lambda: h.sent("session.thinking.append"))
    assert "security check" in h.sent("session.thinking.append")[0]["content"]
    h.link.push(closed())
    await h.finish()


async def test_private_detail_in_agent_speech_is_stopped_and_flagged(tmp_path: Path) -> None:
    h = Harness(tmp_path, private_terms="Žirmūnų")
    await h.start()
    h.link.push(agent_says("Jis gyvena Žirmūnų gatvėje."), caller_says("Ačiū."))
    await until(lambda: h.instructions())
    assert "private detail" in h.instructions()[0]
    assert h.call.flagged
    h.link.push(closed())
    await h.finish()
