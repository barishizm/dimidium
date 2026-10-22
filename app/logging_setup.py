"""Structured (JSON) logging with partial phone-number redaction."""

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


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_phone_numbers(value)
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact_value(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
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
        return json.dumps({"ts": ts, **_redact_value(payload)}, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Route uvicorn's loggers through the same handler.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True


def setup_pipecat_logging(level: str = "WARNING") -> None:
    """Send Pipecat's loguru output through our JSON handler (and its number redaction)."""
    from loguru import logger as loguru_logger

    target = logging.getLogger("pipecat")

    def sink(message: Any) -> None:
        record = message.record
        # loguru level numbers line up with logging's (DEBUG 10, INFO 20, WARNING 30, ...).
        target.log(record["level"].no, "%s", record["message"])

    loguru_logger.remove()
    loguru_logger.add(sink, level=level.upper(), format="{message}")
