"""What one phone call does towards the owner: tool handlers, the message store, Telegram."""

import asyncio
import logging
from contextlib import suppress

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
from app.post_call import CallFacts, CallSummary, format_report, needs_attention
from app.telephony.agent_service import AgentBrainService, Mode

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
        # For the end-of-call report.
        self.flagged = False
        self.hung_up_on_hold = False
        self.messages_left = 0

    @property
    def _push(self) -> OwnerChannel | None:
        """The channel, unless this call must stay quiet (answer mode "off")."""
        return self._channel if self._session.notify_owner else None

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
        if self._session.message_only:
            # Voicemail: the brain already refuses, this is the second lock.
            return AskOwnerResult(decision_text_en="", timed_out=True)
        if self._ask is None:
            # No Telegram configured: behave as if the owner did not answer (take a message).
            logger.warning("ask_owner without an owner channel; taking a message instead")
            return AskOwnerResult(decision_text_en="", timed_out=True)
        self._session.collected_info["owner_question"] = args.summary_tr
        # Entering the hold does not wait for "please hold" to finish: the Telegram request goes
        # out right away and the music starts once the agent has stopped speaking.
        async with self._agent.hold() as heard:
            try:
                result = await self._ask(args)
            except asyncio.CancelledError:
                if self._agent.mode is not Mode.CLOSING:
                    # Shielded: the call is being torn down, the owner should still hear of it.
                    with suppress(Exception):
                        await asyncio.shield(self._report_hang_up(args, list(heard)))
                raise
        return result.model_copy(update={"caller_said_during_hold": list(heard)})

    async def _report_hang_up(self, args: AskOwnerInput, heard: list[str]) -> None:
        """The caller hung up on hold: send what was collected as a normal message."""
        assert self._channel is not None
        self.hung_up_on_hold = True
        lines = [f"📵 Arayan beklerken kapattı {self.tag}", args.summary_tr]
        lines.append(f'🗣 "{args.caller_said_lt}"')
        if heard:
            lines.append("Beklerken: " + " / ".join(f'"{h}"' for h in heard))
        await self._channel.send_info("\n".join(lines))

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
        self.flagged = True
        await self.alert(f"Şüpheli arama: {args.reason}")

    async def save_note(self, purpose: str, details: str) -> None:
        """Store a message for the owner and push it to Telegram (unless the call is quiet;
        then it waits for /messages)."""
        text = f"{purpose}\n{details}".strip()
        self.messages_left += 1
        delivered = False
        if self._push is not None:
            delivered = await self._push.send_info(f"📝 Mesaj {self.tag}\n{text}")
        try:
            await db.add_owner_message(self._db_path, self._session.call_sid, text, delivered)
        except Exception:
            logger.exception("Could not store the owner message")

    async def alert(self, text: str) -> None:
        if self._push is not None:
            await self._push.send_info(f"⚠️ {self.tag}\n{text}")

    def facts(self, end_reason: str | None) -> CallFacts:
        return CallFacts(
            end_reason=end_reason,
            owner_decision=self._session.owner_decision,
            messages_left=self.messages_left,
            flagged=self.flagged,
            message_only=self._session.message_only,
            hung_up_on_hold=self.hung_up_on_hold,
        )

    async def send_report(
        self, facts: CallFacts, summary: CallSummary | None, latency: dict[str, int]
    ) -> None:
        """The end-of-call report: silent for routine calls, a normal ping when it matters."""
        if self._push is None:
            return
        text = format_report(self.tag, self._session, facts, summary, latency)
        await self._push.send_info(text, silent=not needs_attention(facts, summary))
