"""Core data models."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

Speaker = Literal["caller", "agent", "system"]


class CallStatus(StrEnum):
    RINGING = "ringing"
    IN_PROGRESS = "in_progress"
    ON_HOLD = "on_hold"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class TranscriptTurn:
    speaker: Speaker
    text: str
    at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class CallSession:
    call_sid: str
    caller_number: str
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    transcript: list[TranscriptTurn] = field(default_factory=list)
    status: CallStatus = CallStatus.RINGING
    collected_info: dict[str, Any] = field(default_factory=dict)
    owner_decision: str | None = None
    ended_at: datetime | None = None
    # One-time secret handed to Twilio in the TwiML; the media stream must present it.
    stream_token: str | None = None
