"""Twilio endpoints: the voice webhook (TwiML) and the media-stream WebSocket."""

import asyncio
import hmac
import logging
import secrets

from fastapi import APIRouter, Request, Response, WebSocket, status
from pipecat.runner.utils import parse_telephony_websocket

from app.config import get_settings
from app.models import CallSession, CallStatus
from app.session_registry import CALL_SESSION_REGISTRY
from app.telephony.pipeline import TelephonyDeps, run_call
from app.telephony.twilio_webhook import STREAM_PATH, is_valid_signature, say_twiml, stream_twiml

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/twilio", tags=["twilio"])

# A call whose media stream has not connected by then is dropped from the registry.
STREAM_CONNECT_TIMEOUT_SECONDS = 30
_HANDSHAKE_TIMEOUT_SECONDS = 10
_UNAVAILABLE_TEXT = "Sorry, the assistant is not available right now. Goodbye."
_XML = "application/xml"


@router.post("/voice")
async def voice(request: Request) -> Response:
    settings = get_settings()
    form = await request.form()
    params = {key: [str(v) for v in form.getlist(key)] for key in form}

    if settings.twilio_validate_signature:
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
            logger.warning("Rejected /twilio/voice request with an invalid signature")
            return Response(status_code=status.HTTP_403_FORBIDDEN)

    call_sid = str(form.get("CallSid") or "")
    caller = str(form.get("From") or "unknown")
    if not call_sid:
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    deps: TelephonyDeps = request.app.state.telephony
    if deps.speech_problem:
        logger.error("Call %s rejected: speech not configured (%s)", call_sid, deps.speech_problem)
        return Response(say_twiml(_UNAVAILABLE_TEXT, language="en-US"), media_type=_XML)

    session = CALL_SESSION_REGISTRY.get(call_sid)
    if session is None:
        session = CallSession(call_sid=call_sid, caller_number=caller)
        CALL_SESSION_REGISTRY.add(session)
    token = secrets.token_urlsafe(24)
    session.stream_token = token
    asyncio.get_running_loop().call_later(
        STREAM_CONNECT_TIMEOUT_SECONDS, _expire_unconnected, call_sid, token
    )
    logger.info(
        "Incoming call",
        extra={"call_sid": call_sid, "caller": caller, "forwarded_from": form.get("ForwardedFrom")},
    )
    twiml = stream_twiml(
        settings.public_ws_base_url + STREAM_PATH,
        {"from_number": caller, "stream_token": token},
    )
    return Response(twiml, media_type=_XML)


def _expire_unconnected(call_sid: str, token: str) -> None:
    session = CALL_SESSION_REGISTRY.get(call_sid)
    if session is not None and session.stream_token == token:
        CALL_SESSION_REGISTRY.remove(call_sid)
        logger.warning("Media stream never connected", extra={"call_sid": call_sid})


@router.websocket("/stream")
async def stream(websocket: WebSocket) -> None:
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
