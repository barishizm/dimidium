"""ask_owner during a live call, end to end: Twilio WebSocket -> pipeline -> hold -> Telegram.

The fake Twilio client over the WebSocket is the transport; STT, TTS, the Anthropic client and
the Telegram bot are fakes. Everything else is real: Pipecat, AgentBrain with its tool loop,
the hold manager, OwnerChannel and the end-of-call bookkeeping.

Fixed phrases are cached as constant mu-law bytes (one value per phrase), so the test can tell
from the audio the caller received which phrase played.
"""

import base64
import queue
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from app.agent.brain import AgentBrain, BrainConfig
from app.answer_mode import AnswerMode
from app.config import get_settings
from app.main import app
from app.owner_channel import OwnerChannel
from app.speech.base import STTConfig, STTProvider, TranscriptEvent, TTSProvider, VoiceConfig
from app.speech.phrases_lt import PHRASES_BY_KEY
from app.telephony.twilio_webhook import compute_signature
from tests.test_brain import FakeClient, message, text, tool_use

OWNER = 4242
BASE_URL = "https://abc.ngrok-free.app"
# One mu-law byte value per cached phrase; 0x66 is anything the fake TTS synthesizes live.
PHRASE_BYTES = {
    "greeting": 0x55,
    "greeting_voicemail": 0x44,
    "hold": 0x33,
    "still_trying": 0x11,
    "hold_still_here": 0x22,
}
LIVE_TTS_BYTE = 0x66
SILENCE = base64.b64encode(b"\xff" * 160).decode()

ASK = {
    "summary_tr": "DPD kuryesi paket getirdi, kimse kapıyı açmıyor.",
    "caller_said_lt": "Kur palikti siuntinį?",
    "options": [
        {"id": "door", "label_tr": "Kapıya bırak", "meaning_en": "Leave the parcel at the door."},
        {"id": "neighbour", "label_tr": "Komşuya", "meaning_en": "Give it to the neighbour."},
    ],
}


def phrase_text(key: str) -> str:
    return PHRASES_BY_KEY[key].text


# --- fakes ------------------------------------------------------------------------------------


class QueueSTT(STTProvider):
    """Emits a final transcript for each text the test puts in the queue."""

    name = "fake"

    def __init__(self, texts: "queue.Queue[str]") -> None:
        self._texts = texts

    async def stream(
        self, audio: AsyncIterator[bytes], config: STTConfig
    ) -> AsyncIterator[TranscriptEvent]:
        async for _ in audio:
            try:
                said = self._texts.get_nowait()
            except queue.Empty:
                continue
            yield TranscriptEvent(said, is_final=True, language="lt-LT")


class RecordingTTS(TTSProvider):
    """Live TTS: 0.25 s of audio per sentence; "please hold" sentences last 2 s."""

    name = "fake"

    def __init__(self) -> None:
        self.started: list[tuple[float, str]] = []

    async def synthesize(self, text: str, voice: VoiceConfig) -> AsyncIterator[bytes]:
        self.started.append((time.monotonic(), text))
        seconds = 2.0 if "palaukti" in text else 0.25
        yield bytes([LIVE_TTS_BYTE]) * int(8000 * seconds)


@dataclass
class FakeBot:
    sent: list[dict[str, Any]] = field(default_factory=list)
    edits: list[dict[str, Any]] = field(default_factory=list)

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        self.sent.append({"at": time.monotonic(), "chat_id": chat_id, "text": text, **kwargs})
        return SimpleNamespace(message_id=len(self.sent))

    async def edit_message_text(self, text: str, **kwargs: Any) -> Any:
        self.edits.append({"text": text, **kwargs})

    async def edit_message_reply_markup(self, **kwargs: Any) -> Any:
        return None

    @property
    def requests(self) -> list[dict[str, Any]]:
        """Decision requests (messages with buttons)."""
        return [m for m in self.sent if m.get("reply_markup") is not None]

    def texts(self) -> list[str]:
        return [m["text"] for m in self.sent]


@dataclass
class Harness:
    client: TestClient
    llm: FakeClient
    bot: FakeBot
    channel: OwnerChannel
    tts: RecordingTTS
    stt_texts: "queue.Queue[str]"


def tool_results(request: dict[str, Any]) -> list[dict[str, Any]]:
    last = request["messages"][-1]
    return [b for b in last["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]


def wait_until(condition: Callable[[], bool], timeout: float = 8.0, what: str = "") -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what or condition}")
        time.sleep(0.02)


@pytest.fixture
def make_harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., Harness]]:
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE_URL)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "secret")
    monkeypatch.setenv("HOLD_PHRASE_INTERVAL_SECONDS", "0.6")
    monkeypatch.setenv("TURN_SPEECH_TIMEOUT", "0.2")
    monkeypatch.setenv("FILLER_AFTER_SECONDS", "10")
    monkeypatch.setenv("CALLER_IDLE_SECONDS", "30")
    monkeypatch.setattr("app.main.check_speech", lambda settings: None)

    async def warm(settings: object, phrases: Any) -> None:
        for key, value in PHRASE_BYTES.items():
            seconds = 0.5 if key.startswith("greeting") else 0.3
            phrases.put_mulaw(phrase_text(key), bytes([value]) * int(8000 * seconds))

    monkeypatch.setattr("app.main.warm_phrases", warm)
    clients: list[TestClient] = []

    def make(*responses: SimpleNamespace, hold_timeout: float = 10) -> Harness:
        monkeypatch.setenv("HOLD_TIMEOUT_SECONDS", str(hold_timeout))
        bot = FakeBot()
        channel = OwnerChannel(bot, OWNER)
        llm = FakeClient(*responses)
        tts = RecordingTTS()
        stt_texts: queue.Queue[str] = queue.Queue()

        async def start_telegram(settings: object) -> Any:
            async def stop() -> None:
                return None

            return SimpleNamespace(channel=channel, stop=stop)

        def create_brain(
            settings: Any, handlers: Any, rules: Any, number: Any, session: Any
        ) -> Any:
            config = BrainConfig(
                model="test-model",
                hold_timeout_seconds=settings.hold_timeout_seconds,
                # Like the real create_brain: a voicemail session never asks the owner.
                max_owner_questions=0 if session.message_only else settings.max_owner_questions,
                owner_grace_seconds=2,
            )
            return AgentBrain(llm, config, handlers, [], session)  # type: ignore[arg-type]

        monkeypatch.setattr("app.main.start_telegram", start_telegram)
        monkeypatch.setattr("app.telephony.pipeline.create_brain", create_brain)
        monkeypatch.setattr("app.telephony.pipeline.create_stt", lambda s: QueueSTT(stt_texts))
        monkeypatch.setattr("app.telephony.pipeline.create_tts", lambda s: tts)
        get_settings.cache_clear()
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return Harness(client, llm, bot, channel, tts, stt_texts)

    yield make
    for client in clients:
        client.__exit__(None, None, None)


# --- the fake Twilio side ---------------------------------------------------------------------


class FakeTwilioCall:
    """Plays Twilio: streams caller audio every 20 ms and collects the audio sent back."""

    def __init__(self, harness: Harness, call_sid: str) -> None:
        self._harness = harness
        self.call_sid = call_sid
        self.received = bytearray()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ws: WebSocketTestSession | None = None
        self._threads: list[threading.Thread] = []
        self._context: Any = None

    def __enter__(self) -> "FakeTwilioCall":
        client = self._harness.client
        form = {"CallSid": self.call_sid, "From": "+37060000000", "To": "+37052000000"}
        signature = compute_signature(
            f"{BASE_URL}/twilio/voice", {k: [v] for k, v in form.items()}, "secret"
        )
        twiml = client.post("/twilio/voice", data=form, headers={"X-Twilio-Signature": signature})
        stream = ET.fromstring(twiml.text).find("Connect/Stream")
        assert stream is not None
        params = {p.get("name"): p.get("value") for p in stream.findall("Parameter")}

        stream_signature = compute_signature(
            BASE_URL.replace("https://", "wss://") + "/twilio/stream", {}, "secret"
        )
        self._context = client.websocket_connect(
            "/twilio/stream", headers={"X-Twilio-Signature": stream_signature}
        )
        self._ws = ws = self._context.__enter__()
        ws.send_json({"event": "connected", "protocol": "Call", "version": "1.0.0"})
        ws.send_json(
            {
                "event": "start",
                "streamSid": "MZ1",
                "start": {
                    "streamSid": "MZ1",
                    "callSid": self.call_sid,
                    "customParameters": {
                        "from_number": "+37060000000",
                        "stream_token": params["stream_token"],
                    },
                    "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
                },
            }
        )
        for target in (self._send_loop, self._receive_loop):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

    def __exit__(self, *exc: object) -> None:
        self.hang_up()

    def _send_loop(self) -> None:
        assert self._ws is not None
        while not self._stop.is_set():
            try:
                self._ws.send_json(
                    {"event": "media", "streamSid": "MZ1", "media": {"payload": SILENCE}}
                )
            except Exception:
                return
            time.sleep(0.02)

    def _receive_loop(self) -> None:
        assert self._ws is not None
        while True:
            try:
                message = self._ws.receive_json()
            except Exception:
                self._stop.set()
                return
            if message.get("event") == "media":
                with self._lock:
                    self.received += base64.b64decode(message["media"]["payload"])

    def heard(self, key: str, seconds: float = 0.1) -> bool:
        """True if the caller received at least `seconds` of the given phrase's audio."""
        run = bytes([PHRASE_BYTES[key]]) * int(8000 * seconds)
        with self._lock:
            return run in self.received

    def heard_live_tts(self) -> bool:
        with self._lock:
            return bytes([LIVE_TTS_BYTE]) * 800 in self.received

    def say(self, text: str) -> None:
        self._harness.stt_texts.put(text)

    def owner(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run an owner-channel coroutine on the call's event loop (like a Telegram update)."""
        assert self._ws is not None
        return self._ws.portal.call(fn, *args)

    @property
    def ended_by_server(self) -> bool:
        return self._stop.is_set()

    def hang_up(self) -> None:
        if self._ws is None:
            return
        self._stop.set()
        with suppress(Exception):
            self._ws.send_json({"event": "stop", "streamSid": "MZ1"})
        with suppress(Exception):
            self._context.__exit__(None, None, None)
        self._ws = None
        for thread in self._threads:
            thread.join(timeout=2)


def click(call: FakeTwilioCall, harness: Harness, request_index: int, button: int) -> None:
    request = harness.bot.requests[request_index]
    data = request["reply_markup"].inline_keyboard[button][0].callback_data
    message_id = harness.bot.sent.index(request) + 1
    answer = call.owner(harness.channel.handle_callback, OWNER, OWNER, data, message_id)
    assert answer == "Karar iletildi."


def wait_for_greeting(call: FakeTwilioCall) -> None:
    wait_until(lambda: call.heard("greeting", 0.45), what="greeting")
    # The caller is muted until the disclosure has finished playing out.
    time.sleep(0.3)


def summary_sent(harness: Harness) -> bool:
    return any("Arama bitti" in t.split("\n", 1)[0] for t in harness.bot.texts())


def summary_message(harness: Harness) -> dict[str, Any]:
    return next(m for m in harness.bot.sent if "Arama bitti" in m["text"].split("\n", 1)[0])


def db_rows(sql: str) -> list[tuple[Any, ...]]:
    with sqlite3.connect(get_settings().db_path) as db:
        return db.execute(sql).fetchall()


# --- scenarios --------------------------------------------------------------------------------


def test_owner_button_decision_is_relayed(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness(
        message(text("Prašau trumpam palaukti, nepadėkite ragelio."), tool_use("ask_owner", ASK)),
        message(
            text("Savininkas prašo palikti siuntinį prie durų. Viso gero!"),
            tool_use("end_call", {"reason": "decision relayed"}, "tu_2"),
        ),
    )
    with FakeTwilioCall(harness, "CA1001") as call:
        wait_for_greeting(call)
        call.say("Laba diena, čia DPD kurjeris, kur palikti siuntinį?")
        wait_until(lambda: len(harness.bot.requests) == 1, what="Telegram request")

        # Sent as soon as the tool was called, not after the 2 s "please hold" finished playing
        # (it may even go out before TTS starts on that sentence).
        wait_until(lambda: any("palaukti" in said for _, said in harness.tts.started))
        hold_tts = next(at for at, said in harness.tts.started if "palaukti" in said)
        assert abs(harness.bot.requests[0]["at"] - hold_tts) < 1.0
        assert harness.bot.requests[0]["chat_id"] == OWNER
        assert ASK["summary_tr"] in harness.bot.requests[0]["text"]

        # On hold: music, then the periodic phrase. The caller talks; the LLM stays out of it.
        wait_until(lambda: call.heard("still_trying"), what="still-trying phrase")
        call.say("Alio? Ar jūs dar ten?")
        wait_until(lambda: call.heard("hold_still_here"), what="canned hold reply")
        assert len(harness.llm.requests) == 1

        click(call, harness, 0, 0)
        wait_until(lambda: len(harness.llm.requests) == 2, what="decision sent to the LLM")
        result = tool_results(harness.llm.requests[1])[0]["content"]
        assert result.startswith("OWNER_DECISION: Leave the parcel at the door.")
        assert "CALLER_SAID_DURING_HOLD" in result and "Alio? Ar jūs dar ten?" in result

        wait_until(lambda: call.ended_by_server, what="agent ending the call")
    wait_until(lambda: summary_sent(harness), what="end-of-call summary")

    assert "✅ Karar: Kapıya bırak" in harness.bot.edits[0]["text"]
    assert not call.heard("hold")  # the model said "please hold" itself; nothing injected
    caller_lines = [
        t for s, t in db_rows("SELECT speaker, text FROM transcript_turns") if s == "caller"
    ]
    assert "Alio? Ar jūs dar ten?" in caller_lines
    assert db_rows("SELECT status FROM calls") == [("completed",)]


def test_free_text_decision_and_injected_hold_phrase(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness(
        # No "please hold" from the model: the code must add it before asking the owner.
        message(tool_use("ask_owner", ASK)),
        message(
            text("Savininkas sako, kad siuntinį galite palikti kaimynui. Viso gero!"),
            tool_use("end_call", {"reason": "decision relayed"}, "tu_2"),
        ),
    )
    with FakeTwilioCall(harness, "CA1002") as call:
        wait_for_greeting(call)
        call.say("Turiu siuntinį jums.")
        wait_until(lambda: len(harness.bot.requests) == 1, what="Telegram request")
        wait_until(lambda: call.heard("hold", 0.25), what="injected hold phrase")

        reply = call.owner(harness.channel.handle_text, OWNER, OWNER, "Komşuya bırakabilir")
        assert reply is not None and reply.startswith("✅ İletildi")
        wait_until(lambda: len(harness.llm.requests) == 2, what="decision sent to the LLM")
        result = tool_results(harness.llm.requests[1])[0]["content"]
        assert result.startswith("OWNER_DECISION: The owner replied in free text")
        assert "OWNER_ORIGINAL_TEXT: Komşuya bırakabilir" in result
        wait_until(lambda: call.ended_by_server, what="agent ending the call")
    wait_until(lambda: summary_sent(harness), what="end-of-call summary")

    assert "Cevabınız: Komşuya bırakabilir" in harness.bot.edits[0]["text"]
    agent_lines = [
        t for s, t in db_rows("SELECT speaker, text FROM transcript_turns") if s == "agent"
    ]
    assert any(phrase_text("hold") in line for line in agent_lines)


def test_owner_timeout_takes_a_message(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness(
        message(text("Prašau palaukti, nepadėkite ragelio."), tool_use("ask_owner", ASK)),
        message(
            text("Atsiprašau, jis neatsiliepia."),
            tool_use(
                "save_message",
                {"caller_name": "Jonas", "caller_org": "DPD", "purpose": "Paket teslimi"},
                "tu_2",
            ),
        ),
        message(
            text("Žinutę perduosiu. Viso gero!"),
            tool_use("end_call", {"reason": "timeout"}, "tu_3"),
        ),
        hold_timeout=1.5,
    )
    with FakeTwilioCall(harness, "CA1003") as call:
        wait_for_greeting(call)
        call.say("Čia Jonas iš DPD.")
        wait_until(lambda: len(harness.bot.requests) == 1, what="Telegram request")
        # Nobody answers within the 1.5 s hold timeout.
        wait_until(lambda: len(harness.llm.requests) >= 2, what="timeout result")
        assert tool_results(harness.llm.requests[1])[0]["content"].startswith("OWNER_TIMEOUT")
        wait_until(lambda: call.ended_by_server, what="agent ending the call")
    wait_until(lambda: summary_sent(harness), what="end-of-call summary")

    assert "⏱ Süre doldu" in harness.bot.edits[0]["text"]
    assert any(t.startswith("📝 Mesaj") and "Paket teslimi" in t for t in harness.bot.texts())
    assert len(db_rows("SELECT message FROM messages_for_owner")) == 1
    # A message was left: the report rings (not silent) and is marked for follow-up.
    report = summary_message(harness)
    assert report["text"].startswith("📌") and report["disable_notification"] is False
    assert db_rows("SELECT follow_up, end_reason FROM calls") == [(1, "timeout")]


def test_caller_hangs_up_during_hold(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness(
        message(text("Prašau palaukti, nepadėkite ragelio."), tool_use("ask_owner", ASK)),
    )
    call = FakeTwilioCall(harness, "CA1004").__enter__()
    wait_for_greeting(call)
    call.say("Kur palikti siuntinį?")
    wait_until(lambda: len(harness.bot.requests) == 1, what="Telegram request")
    call.say("Gerai, palauksiu.")
    time.sleep(0.6)
    call.hang_up()

    wait_until(
        lambda: any("📵 Arayan kapattı" in e["text"] for e in harness.bot.edits),
        what="request edited to 'caller hung up'",
    )
    wait_until(
        lambda: any(t.startswith("📵 Arayan beklerken kapattı") for t in harness.bot.texts()),
        what="collected info sent",
    )
    report = next(t for t in harness.bot.texts() if t.startswith("📵 Arayan beklerken kapattı"))
    assert ASK["summary_tr"] in report and "Gerai, palauksiu." in report
    assert harness.channel.pending_call_sids == []
    assert len(harness.llm.requests) == 1  # the model was never resumed
    wait_until(lambda: summary_sent(harness), what="end-of-call summary")


def test_second_ask_allowed_third_refused(make_harness: Callable[..., Harness]) -> None:
    second_ask = {**ASK, "summary_tr": "Kurye komşu evde değil diyor."}
    harness = make_harness(
        message(text("Prašau palaukti, nepadėkite ragelio."), tool_use("ask_owner", ASK)),
        # The second question comes without a hold sentence: the code adds one.
        message(text("Kaimyno nėra namuose."), tool_use("ask_owner", second_ask, "tu_2")),
        message(tool_use("ask_owner", ASK, "tu_3")),
        message(text("Gerai, viso gero!"), tool_use("end_call", {"reason": "done"}, "tu_4")),
    )
    with FakeTwilioCall(harness, "CA1005") as call:
        wait_for_greeting(call)
        call.say("Ar galiu palikti kaimynui?")
        wait_until(lambda: len(harness.bot.requests) == 1, what="first Telegram request")
        click(call, harness, 0, 1)

        wait_until(lambda: len(harness.bot.requests) == 2, what="second Telegram request")
        assert second_ask["summary_tr"] in harness.bot.requests[1]["text"]
        wait_until(lambda: call.heard("hold", 0.25), what="injected hold phrase")
        click(call, harness, 1, 0)

        wait_until(lambda: len(harness.llm.requests) == 4, what="limit result")
        assert tool_results(harness.llm.requests[3])[0]["content"].startswith("LIMIT")
        wait_until(lambda: call.ended_by_server, what="agent ending the call")
    wait_until(lambda: summary_sent(harness), what="end-of-call summary")

    assert len(harness.bot.requests) == 2  # the third ask never reached Telegram
    decisions = [tool_results(r)[0]["content"] for r in harness.llm.requests[1:3]]
    assert decisions[0].startswith("OWNER_DECISION: Give it to the neighbour.")
    assert decisions[1].startswith("OWNER_DECISION: Leave the parcel at the door.")


def test_off_mode_takes_a_voicemail_quietly(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness(
        # The model tries to ask anyway; the voicemail limit refuses it without a hold.
        message(text("Supratau."), tool_use("ask_owner", ASK)),
        message(
            text("Užrašiau."),
            tool_use("save_message", {"caller_name": "Jonas", "purpose": "Geri arama"}, "tu_2"),
        ),
        message(text("Viso gero!"), tool_use("end_call", {"reason": "voicemail"}, "tu_3")),
    )
    deps = harness.client.app.state.telephony  # type: ignore[attr-defined]
    harness.client.portal.call(deps.modes.set, AnswerMode.OFF)  # type: ignore[union-attr]
    with FakeTwilioCall(harness, "CA1006") as call:
        wait_until(lambda: call.heard("greeting_voicemail", 0.45), what="voicemail greeting")
        time.sleep(0.3)
        call.say("Čia Jonas, prašau perskambinti.")
        wait_until(lambda: call.ended_by_server, what="agent ending the call")
    wait_until(
        lambda: db_rows("SELECT end_reason FROM calls") == [("voicemail",)], what="call stored"
    )

    assert not call.heard("hold", 0.1)
    assert tool_results(harness.llm.requests[1])[0]["content"].startswith("LIMIT")
    # Off mode: nothing reaches Telegram; the message waits, undelivered, for /messages.
    assert harness.bot.sent == [] and harness.bot.requests == []
    assert db_rows("SELECT delivered FROM messages_for_owner") == [(0,)]
