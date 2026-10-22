"""The whole call path over a real WebSocket: webhook -> stream -> pipeline -> greeting -> hangup.

Speech providers and the brain are fakes; everything Pipecat (transport, Twilio serializer,
VAD, aggregators) is real.
"""

import base64
import sqlite3
import time
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.models import CallSession, TranscriptTurn
from app.session_registry import CALL_SESSION_REGISTRY
from app.speech.base import STTConfig, STTProvider, TranscriptEvent, TTSProvider, VoiceConfig
from app.speech.phrases_lt import PHRASES_BY_KEY
from app.telephony.twilio_webhook import compute_signature

GREETING_MULAW = b"\x55" * 4000  # 0.5 s


class SilentSTT(STTProvider):
    name = "fake"

    async def stream(
        self, audio: AsyncIterator[bytes], config: STTConfig
    ) -> AsyncIterator[TranscriptEvent]:
        async for _ in audio:
            pass
        return
        yield  # pragma: no cover


class NoTTS(TTSProvider):
    name = "fake"

    async def synthesize(self, text: str, voice: VoiceConfig) -> AsyncIterator[bytes]:
        raise AssertionError(f"greeting should come from the cache, not TTS: {text}")
        yield b""  # pragma: no cover


class QuietBrain:
    ended = False
    last_turn_failed = False

    def __init__(self, session: CallSession) -> None:
        self._session = session

    def add_opening(self, text: str) -> None:
        # Like AgentBrain.add_opening: the greeting goes into the transcript.
        self._session.transcript.append(TranscriptTurn(speaker="agent", text=text))

    async def respond(self, text: str, event: str | None = None) -> AsyncIterator[str]:
        return
        yield ""  # pragma: no cover


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://abc.ngrok-free.app")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "secret")
    monkeypatch.setattr("app.main.check_speech", lambda settings: None)

    async def warm(settings: object, phrases: object) -> None:
        phrases.put_mulaw(PHRASES_BY_KEY["greeting"].text, GREETING_MULAW)  # type: ignore[attr-defined]

    monkeypatch.setattr("app.main.warm_phrases", warm)
    monkeypatch.setattr("app.telephony.pipeline.create_stt", lambda settings: SilentSTT())
    monkeypatch.setattr("app.telephony.pipeline.create_tts", lambda settings: NoTTS())
    monkeypatch.setattr(
        "app.telephony.pipeline.create_brain",
        lambda settings, handlers, rules, number, session: QuietBrain(session),
    )
    get_settings.cache_clear()
    with TestClient(app) as test_client:
        yield test_client


def test_call_greets_from_cache_and_cleans_up(client: TestClient) -> None:
    form = {"CallSid": "CA42", "From": "+37060000000", "To": "+37052000000"}
    signature = compute_signature(
        "https://abc.ngrok-free.app/twilio/voice", {k: [v] for k, v in form.items()}, "secret"
    )
    twiml = client.post("/twilio/voice", data=form, headers={"X-Twilio-Signature": signature})
    stream = ET.fromstring(twiml.text).find("Connect/Stream")
    assert stream is not None
    token = {p.get("name"): p.get("value") for p in stream.findall("Parameter")}["stream_token"]

    media: list[bytes] = []
    with client.websocket_connect("/twilio/stream") as ws:
        ws.send_json({"event": "connected", "protocol": "Call", "version": "1.0.0"})
        ws.send_json(
            {
                "event": "start",
                "streamSid": "MZ42",
                "start": {
                    "streamSid": "MZ42",
                    "callSid": "CA42",
                    "customParameters": {"from_number": "+37060000000", "stream_token": token},
                    "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
                },
            }
        )
        silence = base64.b64encode(b"\xff" * 160).decode()
        deadline = time.monotonic() + 5
        while sum(map(len, media)) < len(GREETING_MULAW) and time.monotonic() < deadline:
            ws.send_json({"event": "media", "streamSid": "MZ42", "media": {"payload": silence}})
            message = ws.receive_json()
            if message.get("event") == "media":
                assert message["streamSid"] == "MZ42"
                media.append(base64.b64decode(message["media"]["payload"]))
        ws.send_json({"event": "stop", "streamSid": "MZ42"})

    assert sum(map(len, media)) >= len(GREETING_MULAW)
    # The greeting reached the caller as the cached audio (mu-law round trip is lossless here).
    assert b"".join(media)[:4000].count(b"\x55") > 3500

    # End-of-call bookkeeping runs in the background; give it a moment.
    deadline = time.monotonic() + 5
    status = ended = None
    while status != "completed" and time.monotonic() < deadline:
        time.sleep(0.05)
        with sqlite3.connect(get_settings().db_path) as db:
            status, ended = db.execute("SELECT status, ended_at FROM calls").fetchone()
    assert status == "completed" and ended
    assert CALL_SESSION_REGISTRY.get("CA42") is None
    with sqlite3.connect(get_settings().db_path) as db:
        turns = db.execute("SELECT speaker, text FROM transcript_turns").fetchall()
    assert turns == [("agent", PHRASES_BY_KEY["greeting"].text)]
