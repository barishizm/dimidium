"""Forward a live call to the owner's real phone (Twilio REST: redirect the call to <Dial>).

This is the one exception to the single-call-leg rule, and only for allowlisted callers whose
ring alert the owner answered with "forward to my phone".
"""

import logging

import httpx

from app.config import Settings
from app.session_registry import CALL_SESSION_REGISTRY
from app.telephony.twilio_webhook import dial_twiml

logger = logging.getLogger(__name__)

TWILIO_API = "https://api.twilio.com/2010-04-01"
DIAL_DONE_PATH = "/twilio/dial-done"
_TIMEOUT_SECONDS = 10


class CallForwarder:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self._settings = settings
        self._transport = transport

    @property
    def problem(self) -> str | None:
        """None when forwarding can work, else what is missing (Turkish, shown to the owner)."""
        s = self._settings
        if not s.owner_forward_number:
            return "OWNER_FORWARD_NUMBER ayarlı değil."
        if not s.twilio_account_sid or s.twilio_auth_token is None:
            return "Twilio bilgileri (TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN) eksik."
        return None

    def twiml(self) -> str:
        s = self._settings
        assert s.owner_forward_number
        return dial_twiml(
            s.owner_forward_number,
            # Our own number as caller ID: if the owner's phone forwards the call back to
            # Twilio, the webhook recognizes it and does not loop.
            caller_id=s.twilio_phone_number,
            timeout=s.forward_ring_seconds,
            action_url=s.public_base_url.rstrip("/") + DIAL_DONE_PATH,
        )

    async def forward(self, call_sid: str) -> str:
        """Redirect the call; returns a short Turkish outcome. Never raises."""
        if self.problem:
            return f"⚠️ Yönlendirilemedi: {self.problem}"
        session = CALL_SESSION_REGISTRY.get(call_sid)
        if session is None:
            return "📵 Arama artık devam etmiyor."
        if not session.forward_allowed:
            logger.warning("Forward refused: call was not offered forwarding")
            return "⚠️ Bu arama yönlendirilemez."
        s = self._settings
        assert s.twilio_account_sid and s.twilio_auth_token
        url = f"{TWILIO_API}/Accounts/{s.twilio_account_sid}/Calls/{call_sid}.json"
        # Set before the redirect: once Twilio moves the call, our stream closes, and the
        # pipeline must then not hang the (now forwarded) call up.
        session.collected_info["forwarded_to_owner"] = True
        try:
            async with httpx.AsyncClient(
                auth=(s.twilio_account_sid, s.twilio_auth_token.get_secret_value()),
                timeout=_TIMEOUT_SECONDS,
                transport=self._transport,
            ) as client:
                response = await client.post(url, data={"Twiml": self.twiml()})
        except httpx.HTTPError:
            session.collected_info.pop("forwarded_to_owner", None)
            logger.exception("Twilio call redirect failed")
            return "⚠️ Yönlendirilemedi: Twilio'ya ulaşılamadı."
        if response.status_code >= 400:
            session.collected_info.pop("forwarded_to_owner", None)
            logger.error("Twilio call redirect rejected: HTTP %s", response.status_code)
            return f"⚠️ Yönlendirilemedi: Twilio HTTP {response.status_code}."
        logger.info("Call forwarded to the owner", extra={"call_sid": call_sid})
        return "📲 Arama telefonuna yönlendiriliyor."
