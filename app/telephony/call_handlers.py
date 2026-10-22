"""What one phone call does towards the owner: tool handlers, the message store, Telegram."""

import logging
from datetime import UTC, datetime

from app import db
from app.agent.tools import (
    AskOwnerInput,
    AskOwnerResult,
    EndCallInput,
    FlagSuspiciousInput,
    SaveMessageInput,
    ToolHandlers,
)
from app.models import CallSession
from app.owner_channel import OwnerChannel, make_ask_owner_handler, short_call_id
from app.telephony.agent_service import AgentBrainService

logger = logging.getLogger(__name__)


class CallRecorder:
    """Writes the call row and new transcript turns to SQLite. Never raises: a database problem
    must not break the call."""

    def __init__(self, db_path: str, session: CallSession) -> None:
        self._db_path = db_path
        self._session = session
        self._written = 0

    async def flush(self) -> None:
        try:
            await db.upsert_call(self._db_path, self._session)
            turns = self._session.transcript[self._written :]
            await db.add_transcript_turns(self._db_path, self._session.call_sid, turns)
            self._written += len(turns)
        except Exception:
            logger.exception("Could not store call %s", short_call_id(self._session.call_sid))


class CallOwnerBridge:
    def __init__(
        self,
        session: CallSession,
        agent: AgentBrainService,
        channel: OwnerChannel | None,
        db_path: str,
        hold_timeout: float,
    ) -> None:
        self._session = session
        self._agent = agent
        self._channel = channel
        self._db_path = db_path
        self._ask = make_ask_owner_handler(channel, session, hold_timeout) if channel else None

    @property
    def tag(self) -> str:
        return f"#{short_call_id(self._session.call_sid)} · {self._session.caller_number}"

    def tool_handlers(self) -> ToolHandlers:
        return ToolHandlers(
            ask_owner=self._ask_owner,
            save_message=self._save_message,
            end_call=self._end_call,
            flag_suspicious=self._flag_suspicious,
        )

    async def _ask_owner(self, args: AskOwnerInput) -> AskOwnerResult:
        if self._ask is None:
            # No Telegram configured: behave as if the owner did not answer (take a message).
            logger.warning("ask_owner without an owner channel; taking a message instead")
            return AskOwnerResult(decision_text_en="", timed_out=True)
        async with self._agent.hold():
            return await self._ask(args)

    async def _save_message(self, args: SaveMessageInput) -> None:
        details = "\n".join(
            f"{key}: {value}"
            for key, value in args.model_dump(exclude_none=True).items()
            if key != "purpose"
        )
        await self.save_note(args.purpose, details)

    async def _end_call(self, args: EndCallInput) -> None:
        self._agent.request_end(args.reason)

    async def _flag_suspicious(self, args: FlagSuspiciousInput) -> None:
        logger.warning("Suspicious call %s flagged", short_call_id(self._session.call_sid))
        await self.alert(f"Şüpheli arama: {args.reason}")

    async def save_note(self, purpose: str, details: str) -> None:
        """Store a message for the owner and push it to Telegram."""
        text = f"{purpose}\n{details}".strip()
        delivered = False
        if self._channel is not None:
            delivered = await self._channel.send_info(f"📝 Mesaj {self.tag}\n{text}")
        try:
            await db.add_owner_message(self._db_path, self._session.call_sid, text, delivered)
        except Exception:
            logger.exception("Could not store the owner message")

    async def alert(self, text: str) -> None:
        if self._channel is not None:
            await self._channel.send_info(f"⚠️ {self.tag}\n{text}")

    async def send_summary(self, end_reason: str | None, latency: dict[str, int]) -> None:
        if self._channel is None:
            return
        session = self._session
        ended = session.ended_at or datetime.now(UTC)
        seconds = round((ended - session.started_at).total_seconds())
        lines = [f"📴 Arama bitti {self.tag}"]
        if session.owner_decision:
            lines.append(f"Karar: {session.owner_decision}")
        lines.append(
            f"Süre: {seconds // 60}:{seconds % 60:02d} · Konuşma: {len(session.transcript)} satır"
        )
        if end_reason:
            lines.append(f"Bitiş: {end_reason}")
        if latency.get("turns"):
            lines.append(
                f"Gecikme: medyan {latency['p50_ms']} ms, en yüksek {latency['max_ms']} ms"
            )
        await self._channel.send_info("\n".join(lines))
