import xml.etree.ElementTree as ET
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.session_registry import CALL_SESSION_REGISTRY
from app.telephony.twilio_webhook import (
    compute_signature,
    is_valid_signature,
    say_twiml,
    stream_twiml,
)

TOKEN = "12345"
URL = "https://abc.ngrok-free.app/twilio/voice"
PARAMS = {"CallSid": "CA123", "From": "+37060000000", "To": "+37052000000", "Digits": "1234"}
FORM = {k: [v] for k, v in PARAMS.items()}
# Produced by twilio.request_validator.RequestValidator("12345").compute_signature(URL, PARAMS).
TWILIO_REFERENCE_SIGNATURE = "L9I63lN+1onhbOEv5YFm5Vkivd8="


def test_signature_matches_twilio_reference() -> None:
    assert compute_signature(URL, FORM, TOKEN) == TWILIO_REFERENCE_SIGNATURE


def test_signature_accepts_default_port_variant() -> None:
    signed_with_port = compute_signature("https://abc.ngrok-free.app:443/twilio/voice", FORM, TOKEN)
    assert is_valid_signature(URL, FORM, signed_with_port, TOKEN)


@pytest.mark.parametrize(
    "url, form, signature",
    [
        (URL, {**FORM, "From": ["+37069999999"]}, TWILIO_REFERENCE_SIGNATURE),  # tampered
        ("https://evil.example/twilio/voice", FORM, TWILIO_REFERENCE_SIGNATURE),  # other host
        (URL, FORM, None),
        (URL, FORM, ""),
    ],
)
def test_signature_rejects(url: str, form: dict[str, list[str]], signature: str | None) -> None:
    assert not is_valid_signature(url, form, signature, TOKEN)


def test_stream_twiml_escapes_values() -> None:
    xml = stream_twiml("wss://h/twilio/stream", {"from_number": '+370"<&>', "stream_token": "t"})
    stream = ET.fromstring(xml).find("Connect/Stream")
    assert stream is not None and stream.get("url") == "wss://h/twilio/stream"
    params = {p.get("name"): p.get("value") for p in stream.findall("Parameter")}
    assert params == {"from_number": '+370"<&>', "stream_token": "t"}


def test_say_twiml_hangs_up() -> None:
    root = ET.fromstring(say_twiml("a < b"))
    assert root.find("Say").text == "a < b"  # type: ignore[union-attr]
    assert root.find("Hangup") is not None


# --- routes -------------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://abc.ngrok-free.app")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", TOKEN)
    # No speech keys in tests; pretend they are configured so the TwiML path runs.
    monkeypatch.setattr("app.main.check_speech", lambda settings: None)
    monkeypatch.setattr("app.main.warm_phrases", _no_warm)
    get_settings.cache_clear()
    with TestClient(app) as test_client:
        yield test_client
    for session in CALL_SESSION_REGISTRY.active():
        CALL_SESSION_REGISTRY.remove(session.call_sid)


async def _no_warm(settings: object, phrases: object) -> None:
    return None


def test_voice_rejects_bad_signature(client: TestClient) -> None:
    response = client.post("/twilio/voice", data=PARAMS, headers={"X-Twilio-Signature": "bogus"})
    assert response.status_code == 403
    assert CALL_SESSION_REGISTRY.get("CA123") is None


def test_voice_returns_stream_twiml(client: TestClient) -> None:
    response = client.post(
        "/twilio/voice", data=PARAMS, headers={"X-Twilio-Signature": TWILIO_REFERENCE_SIGNATURE}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    stream = ET.fromstring(response.text).find("Connect/Stream")
    assert stream is not None
    assert stream.get("url") == "wss://abc.ngrok-free.app/twilio/stream"
    params = {p.get("name"): p.get("value") for p in stream.findall("Parameter")}
    assert params["from_number"] == "+37060000000"
    session = CALL_SESSION_REGISTRY.get("CA123")
    assert session is not None and session.stream_token == params["stream_token"]


def test_voice_refuses_politely_without_speech(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client.app.state.telephony, "speech_problem", "STT missing key")  # type: ignore[attr-defined]
    response = client.post(
        "/twilio/voice", data=PARAMS, headers={"X-Twilio-Signature": TWILIO_REFERENCE_SIGNATURE}
    )
    assert ET.fromstring(response.text).find("Say") is not None
    assert CALL_SESSION_REGISTRY.get("CA123") is None


def test_stream_rejects_unknown_token(client: TestClient) -> None:
    client.post(
        "/twilio/voice", data=PARAMS, headers={"X-Twilio-Signature": TWILIO_REFERENCE_SIGNATURE}
    )
    with client.websocket_connect("/twilio/stream") as ws:
        ws.send_json({"event": "connected", "protocol": "Call", "version": "1.0.0"})
        ws.send_json(
            {
                "event": "start",
                "streamSid": "MZ1",
                "start": {
                    "streamSid": "MZ1",
                    "callSid": "CA123",
                    "customParameters": {"from_number": "+37060000000", "stream_token": "x"},
                },
            }
        )
        message = ws.receive()
    assert message["type"] == "websocket.close"
    assert message["code"] == 1008
