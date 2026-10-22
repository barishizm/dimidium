"""Twilio webhook helpers: request signature validation and the TwiML that opens the stream."""

import base64
import hashlib
import hmac
from collections.abc import Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit
from xml.sax.saxutils import escape, quoteattr

STREAM_PATH = "/twilio/stream"
_DEFAULT_PORTS = {"https": 443, "http": 80}


def compute_signature(url: str, params: Mapping[str, Sequence[str]], auth_token: str) -> str:
    """Twilio's X-Twilio-Signature: base64(HMAC-SHA1(url + sorted key/value pairs)).

    `params` maps each POST form field to its values (repeated fields keep every value).
    """
    payload = url + "".join(key + value for key in sorted(params) for value in sorted(params[key]))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def _url_variants(url: str) -> list[str]:
    """The URL as given, plus with the default port added or removed (Twilio signs either)."""
    parts = urlsplit(url)
    default_port = _DEFAULT_PORTS.get(parts.scheme)
    variants = [url]
    if default_port is None or not parts.hostname:
        return variants
    if parts.port is None:
        netloc = f"{parts.netloc}:{default_port}"
    elif parts.port == default_port:
        netloc = parts.netloc.rsplit(":", 1)[0]
    else:
        return variants
    variants.append(urlunsplit(parts._replace(netloc=netloc)))
    return variants


def is_valid_signature(
    url: str, params: Mapping[str, Sequence[str]], signature: str | None, auth_token: str
) -> bool:
    if not signature:
        return False
    return any(
        hmac.compare_digest(compute_signature(candidate, params, auth_token), signature)
        for candidate in _url_variants(url)
    )


def stream_twiml(stream_url: str, parameters: Mapping[str, str]) -> str:
    """TwiML that connects the call to our media-stream WebSocket on the same call leg.

    When the stream closes, TwiML has no further verbs, so Twilio ends the call.
    """
    params = "".join(
        f"<Parameter name={quoteattr(name)} value={quoteattr(value)}/>"
        for name, value in parameters.items()
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Connect><Stream url={quoteattr(stream_url)}>{params}</Stream>"
        "</Connect></Response>"
    )


def say_twiml(text: str, language: str = "lt-LT") -> str:
    """Minimal TwiML used when the stream cannot be offered (misconfiguration)."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Say language={quoteattr(language)}>{escape(text)}</Say><Hangup/></Response>"
    )
