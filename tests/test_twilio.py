import xml.etree.ElementTree as ET
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

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
STREAM_URL = "wss://abc.ngrok-free.app/twilio/stream"
STREAM_HEADERS = {"X-Twilio-Signature": compute_signature(STREAM_URL, {}, TOKEN)}
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
    with client.websocket_connect("/twilio/stream", headers=STREAM_HEADERS) as ws:
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


def test_draining_refuses_new_calls_and_health_reports_it(client: TestClient) -> None:
    assert client.get("/health").status_code == 200
    client.app.state.telephony.draining = True  # type: ignore[attr-defined]
    health = client.get("/health")
    assert health.status_code == 503 and health.json() == {"status": "draining"}
    response = client.post(
        "/twilio/voice", data=PARAMS, headers={"X-Twilio-Signature": TWILIO_REFERENCE_SIGNATURE}
    )
    assert ET.fromstring(response.text).find("Say") is not None
    assert CALL_SESSION_REGISTRY.get("CA123") is None


def test_ip_rate_limit_applies_before_the_signature(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.rate_limit import SlidingWindowLimiter

    deps = client.app.state.telephony  # type: ignore[attr-defined]
    monkeypatch.setattr(deps, "ip_limiter", SlidingWindowLimiter(2, 60))
    codes = [
        client.post("/twilio/voice", data=PARAMS, headers={"X-Twilio-Signature": "x"}).status_code
        for _ in range(3)
    ]
    assert codes == [403, 403, 429]


# --- answer modes, allow/deny lists, forwarding ------------------------------------------

AGATE = "+37060000001"
SPAM = "+37060000009"
MOM = "+37060000002"
RULES_YAML = f"""
known_numbers:
  - {{number: "{MOM}", name: Mom, relation: family}}
do_not_answer:
  allow:
    - {{number: "{AGATE}", name: Agate, alert_tr: "Agate arıyor", offer_forward: true}}
  deny:
    - {{number: "{SPAM}", note: spam}}
"""


class AlertChannel:
    def __init__(self) -> None:
        self.alerts: list[tuple[str, str, bool]] = []

    async def send_ring_alert(self, call_sid: str, text: str, *, offer_forward: bool) -> bool:
        self.alerts.append((call_sid, text, offer_forward))
        return True


@pytest.fixture
def routed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> Iterator[tuple[TestClient, AlertChannel]]:
    from pathlib import Path

    Path("rules.yaml").write_text(RULES_YAML, encoding="utf-8")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://abc.ngrok-free.app")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC1")
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+37052000000")
    monkeypatch.setenv("OWNER_FORWARD_NUMBER", "+37069999999")
    monkeypatch.setattr("app.main.check_speech", lambda settings: None)
    monkeypatch.setattr("app.main.warm_phrases", _no_warm)
    get_settings.cache_clear()
    channel = AlertChannel()
    with TestClient(app) as test_client:
        test_client.app.state.telephony.owner_channel = channel  # type: ignore[attr-defined]
        yield test_client, channel
    for session in CALL_SESSION_REGISTRY.active():
        CALL_SESSION_REGISTRY.remove(session.call_sid)


def post(client: TestClient, path: str, **form: str) -> ET.Element:
    signature = compute_signature(
        "https://abc.ngrok-free.app" + path, {k: [v] for k, v in form.items()}, TOKEN
    )
    response = client.post(path, data=form, headers={"X-Twilio-Signature": signature})
    assert response.status_code == 200
    return ET.fromstring(response.text)


def set_mode(client: TestClient, mode: str) -> None:
    from app.answer_mode import AnswerMode

    modes = client.app.state.telephony.modes  # type: ignore[attr-defined]
    client.portal.call(modes.set, AnswerMode(mode))  # type: ignore[union-attr]


def test_denylisted_caller_is_rejected(routed: tuple[TestClient, AlertChannel]) -> None:
    client, _ = routed
    twiml = post(client, "/twilio/voice", CallSid="CA1", From=SPAM)
    assert twiml.find("Reject") is not None
    assert CALL_SESSION_REGISTRY.get("CA1") is None


def test_call_looping_back_from_owner_phone_hangs_up(
    routed: tuple[TestClient, AlertChannel],
) -> None:
    client, _ = routed
    twiml = post(client, "/twilio/voice", CallSid="CA1", From="+37052000000")
    assert twiml.find("Hangup") is not None


@pytest.mark.parametrize(
    ("mode", "caller", "message_only", "notify"),
    [
        ("screen_all", "+37061111111", False, True),
        ("known_only", "+37061111111", True, True),
        ("known_only", MOM, False, True),
        ("off", "+37061111111", True, False),
        ("off", MOM, True, False),
        ("off", AGATE, False, True),
    ],
)
def test_answer_modes(
    routed: tuple[TestClient, AlertChannel],
    mode: str,
    caller: str,
    message_only: bool,
    notify: bool,
) -> None:
    client, _ = routed
    set_mode(client, mode)
    twiml = post(client, "/twilio/voice", CallSid="CA1", From=caller)
    assert twiml.find("Connect/Stream") is not None
    session = CALL_SESSION_REGISTRY.get("CA1")
    assert session is not None
    assert (session.message_only, session.notify_owner) == (message_only, notify)


def test_off_mode_can_reject(
    routed: tuple[TestClient, AlertChannel], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = routed
    set_mode(client, "off")
    monkeypatch.setattr(get_settings(), "off_mode_action", "reject")
    assert post(client, "/twilio/voice", CallSid="CA1", From=MOM).find("Reject") is not None


def test_allowlisted_caller_triggers_ring_alert(routed: tuple[TestClient, AlertChannel]) -> None:
    client, channel = routed
    post(client, "/twilio/voice", CallSid="CA1", From=AGATE)
    client.portal.call(client.app.state.telephony.drain, 1)  # type: ignore[union-attr, attr-defined]
    assert channel.alerts == [("CA1", "📞 Agate arıyor — asistan karşılıyor.", True)]


def test_dial_done(routed: tuple[TestClient, AlertChannel]) -> None:
    client, _ = routed
    answered = post(
        client, "/twilio/dial-done", CallSid="CA1", From=AGATE, DialCallStatus="completed"
    )
    assert answered.find("Hangup") is not None
    missed = post(
        client, "/twilio/dial-done", CallSid="CA1", From=AGATE, DialCallStatus="no-answer"
    )
    assert missed.find("Connect/Stream") is not None
    session = CALL_SESSION_REGISTRY.get("CA1")
    assert session is not None and session.message_only and session.notify_owner


def test_dial_twiml() -> None:
    from app.telephony.twilio_webhook import dial_twiml

    twiml = ET.fromstring(
        dial_twiml("+37069999999", caller_id="+3705", timeout=20, action_url="https://x/d?a=1&b=2")
    )
    dial = twiml.find("Dial")
    assert dial is not None and dial.get("callerId") == "+3705" and dial.get("timeout") == "20"
    assert dial.get("action") == "https://x/d?a=1&b=2"
    assert dial.findtext("Number") == "+37069999999"


def test_caller_rate_limit_exempts_allowlisted(
    routed: tuple[TestClient, AlertChannel], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.rate_limit import SlidingWindowLimiter

    client, _ = routed
    deps = client.app.state.telephony  # type: ignore[attr-defined]
    monkeypatch.setattr(deps, "caller_limiter", SlidingWindowLimiter(1, 600))
    assert post(client, "/twilio/voice", CallSid="CA1", From=MOM).find("Connect") is not None
    assert post(client, "/twilio/voice", CallSid="CA2", From=MOM).find("Reject") is not None
    for sid in ("CA3", "CA4"):
        assert post(client, "/twilio/voice", CallSid=sid, From=AGATE).find("Connect") is not None


def test_concurrent_call_cap(
    routed: tuple[TestClient, AlertChannel], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = routed
    monkeypatch.setenv("MAX_CONCURRENT_CALLS", "1")
    get_settings.cache_clear()
    assert post(client, "/twilio/voice", CallSid="CA1", From=MOM).find("Connect") is not None
    assert post(client, "/twilio/voice", CallSid="CA2", From=MOM).find("Reject") is not None


def test_stream_rejects_missing_or_bad_signature(client: TestClient) -> None:
    client.post(
        "/twilio/voice", data=PARAMS, headers={"X-Twilio-Signature": TWILIO_REFERENCE_SIGNATURE}
    )
    for headers in ({}, {"X-Twilio-Signature": "bogus"}):
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/twilio/stream", headers=headers) as ws,
        ):
            ws.receive_text()
    # The call's one-time stream token is untouched: the real stream can still connect.
    session = CALL_SESSION_REGISTRY.get("CA123")
    assert session is not None and session.stream_token


def test_stream_signature_accepts_default_port_variant() -> None:
    signed = compute_signature("wss://abc.ngrok-free.app:443/twilio/stream", {}, TOKEN)
    assert is_valid_signature(STREAM_URL, {}, signed, TOKEN)
