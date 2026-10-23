"""Inbound SIP calls: the caller number from the SIP INVITE and the accept/reject decision.

Pure logic, no I/O. The decision mirrors the Twilio voice webhook (app/telephony/routes.py):
- a call from our own Twilio number (a forward that looped back): rejected;
- denylisted numbers: rejected (busy);
- server shutting down / too many calls / one number calling too often: rejected;
- allowlisted numbers: always screened, whatever the mode, and the owner is alerted on ring;
- answer mode "off": voicemail (nothing pushed to Telegram), or rejected;
- answer mode "known numbers only": unknown callers leave a voicemail;
- otherwise: the normal screening agent.

The caller number is untrusted metadata (anyone can put anything in a SIP From header). It is
used for screening and display only, never as authority.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from app.answer_mode import AnswerMode
from app.config import OffModeAction
from app.rate_limit import SlidingWindowLimiter
from app.rules import RulesEngine, normalize_number

UNKNOWN_CALLER = "unknown"

# SIP status codes for declined calls (the caller hears busy / unavailable from the carrier).
SIP_BUSY = 486
SIP_TEMPORARILY_UNAVAILABLE = 480

# The user part of a sip:, sips: or tel: URI: sip:+37060000000@host;params -> +37060000000.
_URI_USER = re.compile(r"(?:sips?|tel):([^@;>\s]+)", re.IGNORECASE)
_HIDDEN = {"anonymous", "restricted", "unavailable", "unknown", "private"}
# Prefer the asserted identity the carrier vouches for, then the plain From header.
_NUMBER_HEADERS = ("p-asserted-identity", "from")


def caller_number_from_headers(headers: Iterable[Mapping[str, object]]) -> str:
    """The caller's number from the INVITE headers, or "unknown" when withheld or missing."""
    values: dict[str, str] = {}
    for header in headers:
        name = str(header.get("name", "")).strip().casefold()
        # Keep the first occurrence of each header (the order is preserved by OpenAI).
        values.setdefault(name, str(header.get("value", "")))
    for name in _NUMBER_HEADERS:
        match = _URI_USER.search(values.get(name, ""))
        if match is None:
            continue
        user = match.group(1).strip()
        if not user or user.casefold() in _HIDDEN:
            continue
        number = re.sub(r"[\s().-]", "", user)
        if re.fullmatch(r"\+?\d{3,20}", number):
            return number
    return UNKNOWN_CALLER


@dataclass(frozen=True)
class Screening:
    action: Literal["accept", "reject"]
    # Short English reason for the logs.
    reason: str
    status_code: int = SIP_BUSY
    # Voicemail: the agent only takes a message, the owner is not asked.
    message_only: bool = False
    # False in "off" mode: stored for /calls and /messages, nothing pushed to Telegram.
    notify_owner: bool = True
    # Allowlisted caller: Turkish ring alert for the owner.
    ring_alert: str | None = None


def screen_call(
    caller: str,
    *,
    rules: RulesEngine,
    mode: AnswerMode,
    off_mode_action: OffModeAction,
    own_number: str | None,
    active_calls: int,
    max_calls: int,
    draining: bool,
    caller_limiter: SlidingWindowLimiter | None,
) -> Screening:
    known_caller = caller != UNKNOWN_CALLER
    if known_caller and own_number and normalize_number(caller) == normalize_number(own_number):
        return Screening("reject", "call from our own number (forward loop)")
    if known_caller and rules.is_blocked(caller):
        return Screening("reject", "denylisted caller")
    if draining:
        return Screening("reject", "server shutting down", SIP_TEMPORARILY_UNAVAILABLE)
    if active_calls >= max_calls:
        return Screening("reject", "MAX_CONCURRENT_CALLS reached")

    allowed = rules.allowed(caller) if known_caller else None
    # Withheld numbers share one bucket: they cannot be told apart.
    if (
        allowed is None
        and caller_limiter is not None
        and not caller_limiter.allow(normalize_number(caller) or caller)
    ):
        return Screening("reject", "caller called too often")

    if allowed is not None:
        return Screening(
            "accept",
            "allowlisted caller",
            ring_alert=f"📞 {allowed.alert_text} — asistan karşılıyor.",
        )
    if mode is AnswerMode.OFF:
        if off_mode_action == "reject":
            return Screening("reject", "answer mode off")
        return Screening(
            "accept", "answer mode off: voicemail", message_only=True, notify_owner=False
        )
    if mode is AnswerMode.KNOWN_ONLY and rules.match_number(caller) is None:
        return Screening(
            "accept", "unknown caller in known-only mode: voicemail", message_only=True
        )
    return Screening("accept", "screening")
