"""Scripted, text-only call against the real agent brain (no audio, no Telegram).

Used by the adversarial integration tests: the caller's lines are given in code, the owner
answers ask_owner with a fixed reply, and every tool call is recorded for assertions.
"""

from app.agent.brain import create_brain
from app.agent.tools import (
    AskOwnerInput,
    AskOwnerResult,
    EndCallInput,
    FlagSuspiciousInput,
    SaveMessageInput,
    ToolHandlers,
)
from app.config import Settings
from app.models import CallSession, CallStatus
from app.rules import RulesEngine
from app.speech.phrases_lt import PHRASES_BY_KEY

DEFAULT_OWNER_REPLY = (
    "The owner cannot help with this right now. Politely take a short message and end the call."
)


class ScriptedCall:
    """One simulated call. `turns` holds (caller line, agent reply) pairs."""

    def __init__(
        self,
        settings: Settings,
        rules: RulesEngine,
        caller_number: str = "+37060000000",
        owner_reply: str = DEFAULT_OWNER_REPLY,
    ) -> None:
        self.owner_reply = owner_reply
        self.asked: list[AskOwnerInput] = []
        self.flags: list[str] = []
        self.messages: list[SaveMessageInput] = []
        self.end_reasons: list[str] = []
        self.turns: list[tuple[str, str]] = []
        self.session = CallSession(call_sid="SIMTEST", caller_number=caller_number)
        self.session.status = CallStatus.IN_PROGRESS
        handlers = ToolHandlers(
            ask_owner=self._ask_owner,
            save_message=self._save_message,
            end_call=self._end_call,
            flag_suspicious=self._flag,
        )
        self.brain = create_brain(settings, handlers, rules, caller_number, self.session)
        # As on a live call: the disclosure greeting is spoken from code.
        self.brain.add_opening(PHRASES_BY_KEY["greeting"].text)

    async def say(self, caller_text: str) -> str:
        """The caller says one line; returns everything the agent spoke in reply."""
        sentences = [s async for s in self.brain.respond(caller_text)]
        await self.brain.flush_alerts()
        reply = " ".join(sentences)
        self.turns.append((caller_text, reply))
        return reply

    @property
    def spoken(self) -> str:
        return " ".join(reply for _, reply in self.turns)

    async def _ask_owner(self, args: AskOwnerInput) -> AskOwnerResult:
        self.asked.append(args)
        return AskOwnerResult(decision_text_en=self.owner_reply)

    async def _save_message(self, args: SaveMessageInput) -> None:
        self.messages.append(args)

    async def _end_call(self, args: EndCallInput) -> None:
        self.end_reasons.append(args.reason)

    async def _flag(self, args: FlagSuspiciousInput) -> None:
        self.flags.append(args.reason)
