"""Twilio endpoints: the voice webhook (TwiML) and the media-stream WebSocket.

Who gets the agent is decided here, before any paid speech API runs:
- a call that loops back from the owner's own phone (caller ID = our Twilio number): hang up;
- denylisted numbers: rejected (busy);
- allowlisted numbers: always screened, whatever the mode, and the owner is alerted on ring;
- answer mode "off": voicemail through the agent (nothing pushed to Telegram), or rejected;
- answer mode "known numbers only": unknown callers leave a voicemail;
- otherwise: the normal screening agent.
"""

import asyncio
import hmac
import logging
import secrets

from fastapi import APIRouter, Request, Response, WebSocket, status
from pipecat.runner.utils import parse_telephony_websocket

from app.answer_mode import AnswerMode
from app.config import Settings, get_settings
from app.models import CallSession, CallStatus
from app.rules import normalize_number
from app.session_registry import CALL_SESSION_REGISTRY
from app.telephony.pipeline import TelephonyDeps, run_call
from app.telephony.twilio_webhook import (
    STREAM_PATH,
    hangup_twiml,
    is_valid_signature,
    reject_twiml,
    say_twiml,
    stream_twiml,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/twilio", tags=["twilio"])

# A call whose media stream has not connected by then is dropped from the registry.
STREAM_CONNECT_TIMEOUT_SECONDS = 30
_HANDSHAKE_TIMEOUT_SECONDS = 10
_UNAVAILABLE_TEXT = "Sorry, the assistant is not available right now. Goodbye."
_XML = "application/xml"


async def _signed_form(request: Request, settings: Settings) -> dict[str, list[str]] | Response:
    """The POSTed form, or the error response when the Twilio signature does not check out."""
    form = await request.form()
    params = {key: [str(v) for v in form.getlist(key)] for key in form}
    if not settings.twilio_validate_signature:
        return params
    if settings.twilio_auth_token is None:
        logger.error("TWILIO_AUTH_TOKEN is not set; cannot validate the Twilio signature")
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    # Behind ngrok / a proxy, Twilio signed the public URL, not the one uvicorn sees.
    url = settings.public_base_url.rstrip("/") + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    signature = request.headers.get("X-Twilio-Signature")
    if not is_valid_signature(
        url, params, signature, settings.twilio_auth_token.get_secret_value()
    ):
        logger.warning("Rejected %s request with an invalid signature", request.url.path)
        return Response(status_code=status.HTTP_403_FORBIDDEN)
    return params


def _stream_signature_ok(websocket: WebSocket, settings: Settings) -> bool:
    """Twilio signs the media-stream handshake too: HMAC over the wss URL, no parameters."""
    if not settings.twilio_validate_signature:
        return True
    if settings.twilio_auth_token is None:
        logger.error("TWILIO_AUTH_TOKEN is not set; cannot validate the stream signature")
        return False
    url = settings.public_ws_base_url + websocket.url.path
    if websocket.url.query:
        url += "?" + websocket.url.query
    return is_valid_signature(
        url,
        {},
        websocket.headers.get("x-twilio-signature"),
        settings.twilio_auth_token.get_secret_value(),
    )


def _field(params: dict[str, list[str]], name: str) -> str:
    values = params.get(name)
    return values[0] if values else ""


def _xml(twiml: str) -> Response:
    return Response(twiml, media_type=_XML)


@router.post("/voice")
async def voice(request: Request) -> Response:
    settings = get_settings()
    deps: TelephonyDeps = request.app.state.telephony
    client_ip = request.client.host if request.client else "unknown"
    if deps.ip_limiter is not None and not deps.ip_limiter.allow(client_ip):
        logger.warning("Rate limit: too many /twilio/voice requests from one IP")
        return Response(status_code=status.HTTP_429_TOO_MANY_REQUESTS)
    params = await _signed_form(request, settings)
    if isinstance(params, Response):
        return params
    call_sid = _field(params, "CallSid")
    caller = _field(params, "From") or "unknown"
    if not call_sid:
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    log = {
        "call_sid": call_sid,
        "caller": caller,
        "forwarded_from": _field(params, "ForwardedFrom"),
    }
    own_number = settings.twilio_phone_number
    if own_number and normalize_number(caller) == normalize_number(own_number):
        # Our forward to the owner's phone was diverted back here (busy / unanswered).
        logger.info("Forwarded call came back to the Twilio number; hanging up", extra=log)
        return _xml(hangup_twiml())
    if deps.rules.is_blocked(caller):
        logger.info("Denylisted caller rejected", extra=log)
        return _xml(reject_twiml())
    if deps.draining:
        logger.info("Call refused: server is shutting down", extra=log)
        return _xml(say_twiml(_UNAVAILABLE_TEXT, language="en-US"))
    if len(CALL_SESSION_REGISTRY) >= settings.max_concurrent_calls:
        logger.warning("Call rejected: MAX_CONCURRENT_CALLS reached", extra=log)
        return _xml(reject_twiml())

    allowed = deps.rules.allowed(caller)
    if (
        allowed is None
        and deps.caller_limiter is not None
        and not deps.caller_limiter.allow(normalize_number(caller) or caller)
    ):
        logger.warning("Rate limit: caller called too often, rejected", extra=log)
        return _xml(reject_twiml())

    message_only, notify_owner, forward_allowed = False, True, False
    mode = deps.modes.mode
    if allowed is not None:
        forward_allowed = allowed.offer_forward and deps.forwarder.problem is None
        if deps.owner_channel is not None:
            deps.spawn(
                deps.owner_channel.send_ring_alert(
                    call_sid,
                    f"📞 {allowed.alert_text} — asistan karşılıyor.",
                    offer_forward=forward_allowed,
                )
            )
    elif mode is AnswerMode.OFF:
        if settings.off_mode_action == "reject":
            logger.info("Answer mode off: call rejected", extra=log)
            return _xml(reject_twiml())
        message_only, notify_owner = True, False
    elif mode is AnswerMode.KNOWN_ONLY and deps.rules.match_number(caller) is None:
        message_only = True

    logger.info("Incoming call", extra={**log, "mode": str(mode), "message_only": message_only})
    return _open_stream(
        deps, settings, call_sid, caller, message_only, notify_owner, forward_allowed
    )


@router.post("/dial-done")
async def dial_done(request: Request) -> Response:
    """The forward to the owner's phone ended. Unanswered: the agent takes a message."""
    settings = get_settings()
    params = await _signed_form(request, settings)
    if isinstance(params, Response):
        return params
    call_sid = _field(params, "CallSid")
    if not call_sid:
        return Response(status_code=status.HTTP_400_BAD_REQUEST)
    dial_status = _field(params, "DialCallStatus")
    logger.info("Forward to the owner ended", extra={"call_sid": call_sid, "status": dial_status})
    if dial_status == "completed":
        return _xml(hangup_twiml())
    deps: TelephonyDeps = request.app.state.telephony
    caller = _field(params, "From") or "unknown"
    return _open_stream(deps, settings, call_sid, caller, message_only=True, notify_owner=True)


def _open_stream(
    deps: TelephonyDeps,
    settings: Settings,
    call_sid: str,
    caller: str,
    message_only: bool,
    notify_owner: bool,
    forward_allowed: bool = False,
) -> Response:
    if deps.speech_problem:
        logger.error("Call %s rejected: speech not configured (%s)", call_sid, deps.speech_problem)
        return _xml(say_twiml(_UNAVAILABLE_TEXT, language="en-US"))

    session = CALL_SESSION_REGISTRY.get(call_sid)
    if session is None:
        session = CallSession(call_sid=call_sid, caller_number=caller)
        CALL_SESSION_REGISTRY.add(session)
    session.message_only = message_only
    session.notify_owner = notify_owner
    session.forward_allowed = forward_allowed
    token = secrets.token_urlsafe(24)
    session.stream_token = token
    asyncio.get_running_loop().call_later(
        STREAM_CONNECT_TIMEOUT_SECONDS, _expire_unconnected, call_sid, token
    )
    twiml = stream_twiml(
        settings.public_ws_base_url + STREAM_PATH,
        {"from_number": caller, "stream_token": token},
    )
    return _xml(twiml)


def _expire_unconnected(call_sid: str, token: str) -> None:
    session = CALL_SESSION_REGISTRY.get(call_sid)
    if session is not None and session.stream_token == token:
        CALL_SESSION_REGISTRY.remove(call_sid)
        logger.warning("Media stream never connected", extra={"call_sid": call_sid})


@router.websocket("/stream")
async def stream(websocket: WebSocket) -> None:
    if not _stream_signature_ok(websocket, get_settings()):
        logger.warning("Rejected media stream with an invalid signature")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await websocket.accept()
    try:
        transport_type, call_data = await asyncio.wait_for(
            parse_telephony_websocket(websocket), _HANDSHAKE_TIMEOUT_SECONDS
        )
    except (TimeoutError, ValueError):
        logger.warning("Media stream handshake failed")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    call_sid = call_data.call_id or ""
    session = CALL_SESSION_REGISTRY.get(call_sid)
    token = str(call_data.body.get("stream_token", "")) if call_data.body else ""
    expected = session.stream_token if session is not None else None
    if transport_type != "twilio" or not expected or not hmac.compare_digest(token, expected):
        # Only streams opened by our own TwiML (one-time token) may use the paid APIs.
        logger.warning("Rejected media stream without a valid token")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    assert session is not None
    session.stream_token = None

    deps: TelephonyDeps = websocket.app.state.telephony
    try:
        await run_call(websocket, call_data, session, deps)
    except Exception:
        session.status = CallStatus.FAILED
        logger.exception("Call failed", extra={"call_sid": call_sid})
    finally:
        CALL_SESSION_REGISTRY.remove(call_sid)
