"""Structured (JSON) logging with partial phone-number redaction and secret masking."""

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

# Phone-like sequences: optional +, then 7+ digits possibly separated by spaces, dashes or
# parentheses. Digits glued to letters, ":" or "." (times, decimals, IDs) are not matched.
_PHONE_RE = re.compile(r"(?<![\w:.])\+?\d(?:[\s\-()]*\d){6,}(?![\w:.])")
_KEEP_PREFIX = 4  # e.g. "+370" country code
_KEEP_SUFFIX = 2

# Credential shapes masked even when not configured here (e.g. echoed in a library error).
_SECRET_RE = re.compile(
    r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"  # Telegram bot token
    r"|\bsk-ant-[A-Za-z0-9_-]{10,}"  # Anthropic API key
    r"|(?<=Bearer )[A-Za-z0-9._~+/=-]{10,}"
    r"|(?<=bot)\d{6,12}:[A-Za-z0-9_-]{30,}"  # token inside a Telegram API URL
)
_MASK = "***"

_STANDARD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "color_message"}


def redact_phone_numbers(text: str) -> str:
    """Mask the middle of every phone-like number, keeping prefix and last digits."""

    def _mask(match: re.Match[str]) -> str:
        raw = match.group(0)
        plus = "+" if raw.startswith("+") else ""
        digits = re.sub(r"\D", "", raw)
        keep_prefix = _KEEP_PREFIX - len(plus)
        hidden = len(digits) - keep_prefix - _KEEP_SUFFIX
        if hidden <= 0:
            return "*" * len(raw)
        return f"{plus}{digits[:keep_prefix]}{'*' * hidden}{digits[-_KEEP_SUFFIX:]}"

    return _PHONE_RE.sub(_mask, text)


def redact_secrets(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Mask the given exact secret values and anything shaped like a known credential."""
    for secret in secrets:
        text = text.replace(secret, _MASK)
    return _SECRET_RE.sub(_MASK, text)


def _redact_value(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        return redact_phone_numbers(redact_secrets(value, secrets))
    if isinstance(value, dict):
        return {k: _redact_value(v, secrets) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact_value(v, secrets) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__()
        # Longest first, so a secret containing another is masked whole. Very short values
        # would mask ordinary text; those are left to the credential pattern.
        self._secrets = tuple(
            sorted((s for s in secrets or [] if len(s) >= 8), key=len, reverse=True)
        )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Anything passed via `extra=` becomes a structured field.
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        ts = payload.pop("ts")
        return json.dumps(
            {"ts": ts, **_redact_value(payload, self._secrets)}, ensure_ascii=False, default=str
        )


def setup_logging(level: str = "INFO", secrets: list[str] | None = None) -> None:
    """JSON logs on stdout. `secrets` (exact values) are masked wherever they appear."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Route uvicorn's loggers through the same handler.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    # httpx logs every request URL at INFO (session IDs, Telegram bot URLs); keep it quieter.
    logging.getLogger("httpx").setLevel(max(logging.WARNING, root.level))
