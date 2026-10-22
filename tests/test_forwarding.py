from urllib.parse import parse_qs

import httpx
import pytest

from app.config import Settings
from app.models import CallSession
from app.session_registry import CALL_SESSION_REGISTRY
from app.telephony.forwarding import CallForwarder


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "twilio_account_sid": "AC1",
        "twilio_auth_token": "secret",
        "twilio_phone_number": "+37052000000",
        "owner_forward_number": "+37069999999",
        "public_base_url": "https://abc.ngrok-free.app",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.fixture
def live_call() -> CallSession:
    session = CallSession(call_sid="CA1", caller_number="+37060000001", forward_allowed=True)
    CALL_SESSION_REGISTRY.add(session)
    yield session  # type: ignore[misc]
    CALL_SESSION_REGISTRY.remove("CA1")


async def test_forward_redirects_the_live_call(live_call: CallSession) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"sid": "CA1"})

    forwarder = CallForwarder(settings(), transport=httpx.MockTransport(handler))
    assert "yönlendiriliyor" in await forwarder.forward("CA1")
    [request] = requests
    assert str(request.url).endswith("/Accounts/AC1/Calls/CA1.json")
    twiml = parse_qs(request.content.decode())["Twiml"][0]
    assert "<Number>+37069999999</Number>" in twiml
    assert 'action="https://abc.ngrok-free.app/twilio/dial-done"' in twiml
    assert live_call.collected_info["forwarded_to_owner"] is True


async def test_forward_reports_twilio_errors(live_call: CallSession) -> None:
    forwarder = CallForwarder(
        settings(), transport=httpx.MockTransport(lambda r: httpx.Response(404))
    )
    assert "HTTP 404" in await forwarder.forward("CA1")
    assert "forwarded_to_owner" not in live_call.collected_info


async def test_forward_of_an_ended_call() -> None:
    assert "devam etmiyor" in await CallForwarder(settings()).forward("CA_GONE")


@pytest.mark.parametrize(
    ("overrides", "missing"),
    [
        ({"owner_forward_number": None}, "OWNER_FORWARD_NUMBER"),
        ({"twilio_account_sid": None}, "Twilio"),
    ],
)
async def test_forward_needs_configuration(overrides: dict[str, object], missing: str) -> None:
    forwarder = CallForwarder(settings(**overrides))
    assert forwarder.problem is not None and missing in forwarder.problem
    assert missing in await forwarder.forward("CA1")


@pytest.mark.parametrize("forwarded", [False, True])
async def test_pipeline_end_hangs_up_only_unforwarded_calls(
    forwarded: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pipecat.frames.frames import CancelFrame
    from pipecat.runner.types import CallData

    from app.telephony.pipeline import build_serializer

    session = CallSession(call_sid="CA1", caller_number="+370")
    if forwarded:
        session.collected_info["forwarded_to_owner"] = True
    call_data = CallData(transport_type="twilio", call_id="CA1", stream_id="MZ1", body={})
    serializer = build_serializer(settings(), call_data, session)
    hang_ups: list[bool] = []

    async def hang_up() -> None:
        hang_ups.append(True)

    monkeypatch.setattr(serializer, "_hang_up_call", hang_up)
    assert await serializer.serialize(CancelFrame()) is None
    assert hang_ups == ([] if forwarded else [True])


async def test_failed_forward_clears_the_flag(live_call: CallSession) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    forwarder = CallForwarder(settings(), transport=httpx.MockTransport(boom))
    assert "ulaşılamadı" in await forwarder.forward("CA1")
    assert "forwarded_to_owner" not in live_call.collected_info


async def test_forward_refused_for_a_call_not_offered_forwarding(live_call: CallSession) -> None:
    # A forged or stale forward button for an ordinary (not allowlisted) call does nothing.
    live_call.forward_allowed = False
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    forwarder = CallForwarder(settings(), transport=httpx.MockTransport(handler))
    outcome = await forwarder.forward("CA1")
    assert "yönlendirilemez" in outcome
    assert requests == []
    assert "forwarded_to_owner" not in live_call.collected_info
