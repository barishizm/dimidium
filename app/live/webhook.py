"""Incoming GPT-Live SIP calls: the OpenAI project webhook, screening, accept / reject.

OpenAI POSTs `live.transport.incoming` (data.type "sip", data.session_id, data.sip_headers) when
a call reaches sip:<project id>@sip.api.openai.com. The deprecated `live.call.incoming` is
handled the same way; `realtime.call.incoming` for the same call is ignored (one handler
decides). The signature is verified with the project webhook secret (Standard Webhooks:
webhook-id / webhook-timestamp / webhook-signature, 5-minute tolerance) before anything else.

The webhook answers 200 right away; screening, accept and the call itself run in the
background, so a slow accept never makes OpenAI re-deliver the event.
"""

import json
import logging
import secrets
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Protocol

from fastapi import APIRouter, Request, Response, status
from openai import AsyncOpenAI, InvalidWebhookSignatureError

from app.answer_mode import AnswerModeStore
from app.config import Settings
from app.live.call import CallHost, LiveCall, LiveCallTimings
from app.live.config import build_sip_accept_body
from app.live.sip import UNKNOWN_CALLER, caller_number_from_headers, screen_call
from app.live.transport import (
    CallControl,
    OpenAICallControl,
    OpenAISidebandConnector,
    SidebandConnector,
    create_openai_client,
)
from app.models import CallSession
from app.owner_channel import OwnerChannel
from app.phrases_lt import PHRASES_BY_KEY
from app.post_call import CallSummarizer
from app.rate_limit import SlidingWindowLimiter
from app.rules import RulesEngine
from app.session_registry import CALL_SESSION_REGISTRY

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/openai", tags=["openai"])

INCOMING_CALL_EVENTS = frozenset({"live.transport.incoming", "live.call.incoming"})
# Remembered webhook IDs / session IDs for de-duplication.
_DEDUP_HISTORY = 500
_MAX_BODY_BYTES = 256 * 1024

VOICEMAIL_RULE = (
    "- Voicemail call: the owner cannot be asked on this call (never call ask_owner). Take a "
    "message (name, company, purpose, details, callback number) with save_message, then say "
    "goodbye and call end_call."
)


class LiveHost(CallHost, Protocol):
    # True while the server shuts down: new calls are rejected.
    draining: bool


@dataclass
class LiveDeps:
    settings: Settings
    rules: RulesEngine
    modes: AnswerModeStore
    owner_channel: OwnerChannel | None
    client: AsyncOpenAI
    control: CallControl
    connector: SidebandConnector
    host: LiveHost
    summarizer: CallSummarizer | None = None
    caller_limiter: SlidingWindowLimiter | None = None
    timings: LiveCallTimings | None = None
    _webhook_ids: OrderedDict[str, None] = field(default_factory=OrderedDict)
    _session_ids: OrderedDict[str, None] = field(default_factory=OrderedDict)

    @staticmethod
    def _first(seen: OrderedDict[str, None], key: str) -> bool:
        if key in seen:
            return False
        seen[key] = None
        while len(seen) > _DEDUP_HISTORY:
            seen.popitem(last=False)
        return True

    def first_delivery(self, webhook_id: str) -> bool:
        """False for a re-delivered webhook (same webhook-id)."""
        return self._first(self._webhook_ids, webhook_id)

    def claim_session(self, session_id: str) -> bool:
        """False when this session was already decided (both event names fire for one call)."""
        return self._first(self._session_ids, session_id)


def create_live_deps(
    settings: Settings,
    host: LiveHost,
    *,
    rules: RulesEngine,
    modes: AnswerModeStore,
    owner_channel: OwnerChannel | None,
    summarizer: CallSummarizer | None,
    caller_limiter: SlidingWindowLimiter | None,
) -> LiveDeps | None:
    """None without an OpenAI key (the webhook then answers 503)."""
    if settings.openai_api_key is None:
        logger.warning("OPENAI_API_KEY is not set: GPT-Live SIP calls are disabled")
        return None
    if settings.openai_webhook_secret is None:
        logger.warning("OPENAI_WEBHOOK_SECRET is not set: incoming SIP calls will be refused")
    client = create_openai_client(settings)
    timeout = settings.live_control_timeout_seconds
    return LiveDeps(
        settings=settings,
        rules=rules,
        modes=modes,
        owner_channel=owner_channel,
        client=client,
        control=OpenAICallControl(client, timeout),
        connector=OpenAISidebandConnector(client, timeout),
        host=host,
        summarizer=summarizer,
        caller_limiter=caller_limiter,
    )


@router.post("/webhook")
async def openai_webhook(request: Request) -> Response:
    deps: LiveDeps | None = getattr(request.app.state, "live", None)
    if deps is None:
        logger.error("OpenAI webhook received but GPT-Live is not configured (OPENAI_API_KEY)")
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    secret = deps.settings.openai_webhook_secret
    if secret is None:
        logger.error("OPENAI_WEBHOOK_SECRET is not set; cannot verify the webhook")
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    body = await request.body()
    if len(body) > _MAX_BODY_BYTES:
        return Response(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
    try:
        deps.client.webhooks.verify_signature(
            body, request.headers, secret=secret.get_secret_value()
        )
    except (InvalidWebhookSignatureError, ValueError) as exc:
        # ValueError: a webhook-* header is missing.
        logger.warning("Rejected OpenAI webhook: %s", exc)
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    if not deps.first_delivery(request.headers["webhook-id"]):
        return Response(status_code=status.HTTP_200_OK)
    try:
        event = json.loads(body)
    except ValueError:
        return Response(status_code=status.HTTP_400_BAD_REQUEST)
    kind = event.get("type") if isinstance(event, dict) else None
    if kind not in INCOMING_CALL_EVENTS:
        logger.info("Ignored OpenAI webhook event %s", kind)
        return Response(status_code=status.HTTP_200_OK)

    data = event.get("data") or {}
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        logger.warning("Incoming-call webhook without data.session_id")
        return Response(status_code=status.HTTP_400_BAD_REQUEST)
    if data.get("type", "sip") != "sip":
        logger.warning("Ignored incoming transport of type %r", data.get("type"))
        return Response(status_code=status.HTTP_200_OK)
    if deps.claim_session(session_id):
        headers = data.get("sip_headers")
        deps.host.spawn(
            handle_incoming_call(deps, session_id, headers if isinstance(headers, list) else [])
        )
    return Response(status_code=status.HTTP_200_OK)


def new_call_sid() -> str:
    return f"live-{secrets.token_hex(6)}"


async def handle_incoming_call(
    deps: LiveDeps, session_id: str, sip_headers: list[dict[str, Any]]
) -> None:
    """Screen, accept or reject, then run the call until it ends."""
    settings = deps.settings
    caller = caller_number_from_headers(h for h in sip_headers if isinstance(h, dict))
    screening = screen_call(
        caller,
        rules=deps.rules,
        mode=deps.modes.mode,
        off_mode_action=settings.off_mode_action,
        own_number=settings.twilio_phone_number,
        active_calls=len(CALL_SESSION_REGISTRY),
        max_calls=settings.max_concurrent_calls,
        draining=deps.host.draining,
        caller_limiter=deps.caller_limiter,
    )
    log = {"live_session_id": session_id, "caller": caller, "reason": screening.reason}
    if screening.action == "reject":
        logger.info("Incoming SIP call rejected (%s)", screening.status_code, extra=log)
        try:
            await deps.control.reject(session_id, screening.status_code)
        except Exception:
            logger.exception("Reject request failed", extra=log)
        return

    session = CallSession(
        call_sid=new_call_sid(),
        caller_number=caller,
        message_only=screening.message_only,
        notify_owner=screening.notify_owner,
        live_session_id=session_id,
    )
    log["call_sid"] = session.call_sid
    rules_summary = deps.rules.summary_for_call(caller if caller != UNKNOWN_CALLER else None)
    greeting = None
    if session.message_only:
        rules_summary += "\n" + VOICEMAIL_RULE
        greeting = PHRASES_BY_KEY["greeting_voicemail"].text
    body = build_sip_accept_body(settings, rules_summary, greeting)

    CALL_SESSION_REGISTRY.add(session)
    try:
        try:
            await deps.control.accept(session_id, body["session"])
        except Exception:
            logger.exception("Accept request failed; the call was not answered", extra=log)
            if deps.owner_channel is not None:
                await deps.owner_channel.send_info(
                    f"⚠️ Gelen arama cevaplanamadı ({caller}). OpenAI accept isteği başarısız."
                )
            return
        logger.info("Incoming SIP call accepted", extra={**log, "mode": str(deps.modes.mode)})
        if screening.ring_alert and deps.owner_channel is not None:
            # In the background: the sideband must attach right away. No transfer: the call
            # stays on one leg with the agent.
            deps.host.spawn(deps.owner_channel.send_ring_alert(screening.ring_alert))
        call = LiveCall(
            session,
            settings=settings,
            connector=deps.connector,
            control=deps.control,
            host=deps.host,
            owner_channel=deps.owner_channel,
            summarizer=deps.summarizer,
            timings=deps.timings,
        )
        await call.run()
    finally:
        CALL_SESSION_REGISTRY.remove(session.call_sid)
