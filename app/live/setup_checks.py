"""Pure helpers for the setup checks (scripts/doctor.py): no I/O, so they are unit-tested.

Covers the OpenAI side (project ID format, SIP destination, a locally signed test webhook) and
the Telnyx side (does the number's connection match docs/SETUP_TELNYX.md). Field names for the
Telnyx objects come from Telnyx's OpenAPI spec: phone number (`connection_id`, `status`), voice
settings (`translated_number`), FQDN connection (`active`, `transport_protocol`,
`encrypted_media`, `inbound.codecs`) and FQDN (`fqdn`, `port`, `dns_record_type`).
"""

import base64
import hashlib
import hmac
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.rules import normalize_number

OPENAI_SIP_FQDN = "sip.api.openai.com"
OPENAI_SIP_PORT = 5061
# sip.api.openai.com is the default; the EU host is for projects with EU data residency.
OPENAI_SIP_FQDNS = frozenset({OPENAI_SIP_FQDN, "sip-eu.api.openai.com"})
# Telnyx codec names: G711U is PCMU, G711A is PCMA. OPUS is wideband, G.711 the fallback.
WANTED_CODECS = ("OPUS", "G711U", "G711A")

_PROJECT_ID = re.compile(r"^proj_[A-Za-z0-9_-]{6,}$")
_E164 = re.compile(r"^\+[1-9]\d{6,14}$")


@dataclass(frozen=True)
class Finding:
    check: str
    status: str  # PASS | FAIL | WARN
    detail: str


def valid_project_id(value: str | None) -> bool:
    return bool(value and _PROJECT_ID.match(value))


def valid_e164(value: str | None) -> bool:
    return bool(value and _E164.match(value))


def sip_destination(project_id: str, host: str = OPENAI_SIP_FQDN) -> str:
    """What the Telnyx number's Translated Number turns into on the wire."""
    return f"sip:{project_id}@{host}:{OPENAI_SIP_PORT};transport=tls"


def signed_headers(secret: str, body: str, *, now: float | None = None) -> dict[str, str]:
    """Standard Webhooks headers for `body`, signed like OpenAI signs a delivery."""
    stamp = int(time.time() if now is None else now)
    msg_id, timestamp = f"msg_selftest_{stamp}", str(stamp)
    key = (
        base64.b64decode(secret.removeprefix("whsec_"))
        if secret.startswith("whsec_")
        else secret.encode()
    )
    digest = hmac.new(key, f"{msg_id}.{timestamp}.{body}".encode(), hashlib.sha256).digest()
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": timestamp,
        "webhook-signature": "v1," + base64.b64encode(digest).decode(),
        "content-type": "application/json",
    }


def audit_number(
    records: Sequence[Mapping[str, Any]], number: str
) -> tuple[Mapping[str, Any] | None, Finding]:
    """Find `number` in a Telnyx phone-number listing and check it is active and assigned."""
    wanted = normalize_number(number)
    record = next(
        (r for r in records if normalize_number(r.get("phone_number", "")) == wanted), None
    )
    if record is None:
        return None, Finding(
            "Telnyx number",
            "FAIL",
            f"{number} is not on this Telnyx account (wrong key, or not bought/ported yet)",
        )
    status = record.get("status")
    if status != "active":
        return record, Finding(
            "Telnyx number", "FAIL", f"{number} status is {status!r}, not active"
        )
    if not record.get("connection_id"):
        return record, Finding(
            "Telnyx number",
            "FAIL",
            f"{number} is active but not assigned to a connection (Numbers > My Numbers)",
        )
    return record, Finding(
        "Telnyx number", "PASS", f"{number} active on {record.get('connection_name')!r}"
    )


def audit_connection(
    connection: Mapping[str, Any], fqdns: Sequence[Mapping[str, Any]]
) -> list[Finding]:
    """Check an FQDN connection against docs/SETUP_TELNYX.md (FQDN, TLS, SRTP, codecs)."""
    findings: list[Finding] = []
    name = connection.get("connection_name")

    findings.append(
        Finding("Telnyx connection active", "PASS", f"{name!r} is active")
        if connection.get("active")
        else Finding("Telnyx connection active", "FAIL", f"{name!r} is switched off")
    )

    openai_fqdns = [f for f in fqdns if str(f.get("fqdn", "")).lower() in OPENAI_SIP_FQDNS]
    if not openai_fqdns:
        listed = ", ".join(str(f.get("fqdn")) for f in fqdns) or "none"
        findings.append(
            Finding(
                "Telnyx FQDN",
                "FAIL",
                f"no FQDN {OPENAI_SIP_FQDN} on the connection (has: {listed}): calls will not "
                "reach OpenAI",
            )
        )
    else:
        fqdn = openai_fqdns[0]
        problems = []
        if fqdn.get("port") != OPENAI_SIP_PORT:
            problems.append(f"port is {fqdn.get('port')}, want {OPENAI_SIP_PORT}")
        if str(fqdn.get("dns_record_type", "")).lower() != "a":
            problems.append(f"DNS record type is {fqdn.get('dns_record_type')!r}, want A")
        findings.append(
            Finding("Telnyx FQDN", "FAIL", "; ".join(problems))
            if problems
            else Finding("Telnyx FQDN", "PASS", f"A record {fqdn['fqdn']}:{OPENAI_SIP_PORT}")
        )

    transport = connection.get("transport_protocol")
    findings.append(
        Finding("Telnyx transport", "PASS", "TLS")
        if transport == "TLS"
        else Finding(
            "Telnyx transport", "FAIL", f"transport is {transport!r}, want TLS (port 5061)"
        )
    )

    srtp = connection.get("encrypted_media")
    findings.append(
        Finding("Telnyx SRTP", "PASS", "encrypted media is SRTP")
        if srtp == "SRTP"
        else Finding(
            "Telnyx SRTP",
            "FAIL",
            f"encrypted media is {srtp!r}: set SRTP Mandatory (TLS alone does not encrypt audio)",
        )
    )

    codecs = [str(c).upper() for c in (connection.get("inbound") or {}).get("codecs") or []]
    usable = [c for c in codecs if c in WANTED_CODECS]
    if not usable:
        findings.append(
            Finding(
                "Telnyx codecs",
                "FAIL",
                "none of OPUS / G711U (PCMU) / G711A (PCMA) enabled "
                f"(has: {', '.join(codecs) or 'none'})",
            )
        )
    elif codecs[: len(usable)] != usable:
        findings.append(
            Finding(
                "Telnyx codecs",
                "WARN",
                f"order is {', '.join(codecs)}; put OPUS, G711U, G711A first and untick the rest",
            )
        )
    else:
        findings.append(Finding("Telnyx codecs", "PASS", ", ".join(codecs)))
    return findings


def audit_translated_number(voice: Mapping[str, Any], project_id: str | None) -> Finding:
    """The number's Voice setting "Translated Number" must be the OpenAI project ID."""
    translated = (voice.get("translated_number") or "").strip()
    if not translated:
        return Finding(
            "Telnyx Translated Number",
            "FAIL",
            "empty: set it to your OpenAI project ID (proj_...), or OpenAI never sees the call",
        )
    if project_id and translated != project_id:
        return Finding(
            "Telnyx Translated Number",
            "FAIL",
            f"is {translated!r} but OPENAI_PROJECT_ID is {project_id!r}",
        )
    if not valid_project_id(translated):
        return Finding("Telnyx Translated Number", "FAIL", f"{translated!r} is not a proj_... ID")
    return Finding("Telnyx Translated Number", "PASS", translated)


def audit_call_recording(voice: Mapping[str, Any]) -> Finding:
    """Telnyx can record inbound calls per number; docs/PRIVACY.md says no audio is stored."""
    recording = (voice.get("call_recording") or {}).get("inbound_call_recording_enabled")
    if recording:
        return Finding(
            "Telnyx call recording",
            "WARN",
            "inbound recording is ON for the number: Telnyx would store call audio "
            "(turn it off in the number's Voice settings; see docs/PRIVACY.md)",
        )
    return Finding("Telnyx call recording", "PASS", "off")
