"""Fakes for GPT-Live call tests: sideband link/connector, call control, host, owner channel."""

import asyncio
import itertools
from collections.abc import Awaitable, Callable, Coroutine
from types import SimpleNamespace
from typing import Any

from app.functions import OwnerOption
from app.live.call import LiveCallTimings
from app.live.transport import SidebandDisconnected
from app.owner_channel import DecisionKind, OwnerDecision

_ids = itertools.count(1)

# Everything a call waits for, shrunk so a test call takes well under a second.
FAST = LiveCallTimings(
    disclosure_deadline=0.3,
    goodbye_quiet=0.05,
    goodbye_max=1.0,
    fallback_grace=0.1,
    goodbye_start_timeout=0.2,
    hold_announce_after=0.05,
    hold_phrase_interval=0.1,
    hold_still_trying_max=3,
    response_complete_wait=0.05,
    continuation_stale=1.0,
    reconnect_delay=0.01,
    send_wait=0.3,
    closed_wait=0.3,
    function_timeout=1.0,
    ask_margin=1.0,
    poll=0.01,
)

GREETING_LT = (
    "Laba diena! Atsiliepia dirbtinio intelekto asistentas, nes telefono savininkas šiuo metu "
    "negali kalbėti. Pokalbis įrašomas ir transkribuojamas. Kuo galiu padėti?"
)


def ev(kind: str, **fields: Any) -> dict[str, Any]:
    return {"type": kind, "event_id": f"evt_{next(_ids)}", **fields}


def agent_says(text: str) -> dict[str, Any]:
    return ev("session.output_transcript.delta", delta=text, start_ms=0, end_ms=0)


def caller_says(text: str, end_ms: int = 0) -> dict[str, Any]:
    return ev("session.input_transcript.delta", delta=text, start_ms=0, end_ms=end_ms)


def function_call(name: str, arguments: str, call_id: str = "call_1") -> dict[str, Any]:
    return ev(
        "response.event",
        delegation_id=None,
        event={
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": arguments,
                "status": "completed",
            },
        },
    )


def backend(kind: str, response_id: str, output: list[Any] | None = None) -> dict[str, Any]:
    """A nested lifecycle event; like the real ones, its snapshot output is empty."""
    return ev(
        "response.event",
        delegation_id=None,
        event={"type": kind, "response": {"id": response_id, "output": output or []}},
    )


def backend_response(
    response_id: str, *calls: tuple[str, str, str], complete: bool = True
) -> list[dict[str, Any]]:
    """response.created, one output_item.done per (name, arguments, call_id), completed."""
    events = [backend("response.created", response_id)]
    events += [function_call(name, args, call_id) for name, args, call_id in calls]
    if complete:
        events.append(backend("response.completed", response_id))
    return events


def closed(reason: str = "remote_hangup") -> dict[str, Any]:
    return ev("session.closed", reason=reason, session={}, usage={"seconds": 42})


class FakeLink:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self._queue: asyncio.Queue[dict[str, Any] | Exception | None] = asyncio.Queue()

    def push(self, *events: dict[str, Any]) -> None:
        for event in events:
            self._queue.put_nowait(event)

    def drop(self) -> None:
        self._queue.put_nowait(SidebandDisconnected("connection reset"))

    async def send(self, event: dict[str, Any]) -> None:
        if self.closed:
            raise SidebandDisconnected("closed")
        self.sent.append(event)

    async def recv(self) -> dict[str, Any] | None:
        item = await self._queue.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True
        self._queue.put_nowait(None)


class FakeConnector:
    """Hands out the given links in order; an exception in the list fails that attach."""

    def __init__(self, *links: FakeLink | Exception) -> None:
        self._links = list(links)
        self.attached: list[str] = []
        self.current: FakeLink | None = None
        self._changed = asyncio.Event()

    async def attach(self, session_id: str) -> FakeLink:
        self.attached.append(session_id)
        item = self._links.pop(0)
        if isinstance(item, Exception):
            raise item
        self.current = item
        return item

    async def wait_attached(self, count: int = 1, timeout: float = 2.0) -> None:
        async def attached() -> None:
            while len(self.attached) < count:
                await asyncio.sleep(0.005)

        await asyncio.wait_for(attached(), timeout)


class FakeControl:
    """Records call-control requests; a hangup closes the session like OpenAI does."""

    def __init__(
        self, connector: FakeConnector | None = None, *, fail_accept: bool = False
    ) -> None:
        self.accepted: list[tuple[str, dict[str, Any]]] = []
        self.rejected: list[tuple[str, int]] = []
        self.hangups: list[str] = []
        self._connector = connector
        self._fail_accept = fail_accept

    async def accept(self, session_id: str, session: dict[str, Any]) -> None:
        if self._fail_accept:
            raise RuntimeError("accept failed")
        self.accepted.append((session_id, session))

    async def reject(self, session_id: str, status_code: int) -> None:
        self.rejected.append((session_id, status_code))

    async def hangup(self, session_id: str) -> None:
        self.hangups.append(session_id)
        link = self._connector.current if self._connector else None
        if link is not None and not link.closed:
            link.push(closed("close_requested"))


class FakeHost:
    def __init__(self) -> None:
        self.draining = False
        self.tasks: list[asyncio.Task[None]] = []
        self.tracked: dict[str, Callable[[], Awaitable[None]]] = {}

    def spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        self.tasks.append(asyncio.create_task(coro))

    def track_call(self, call_sid: str, end_politely: Callable[[], Awaitable[None]]) -> None:
        self.tracked[call_sid] = end_politely

    def untrack_call(self, call_sid: str) -> None:
        self.tracked.pop(call_sid, None)

    async def drain(self) -> None:
        while self.tasks:
            await self.tasks.pop(0)


class FakeChannel:
    """Owner channel: answers ask() with `decision` (None = never answers) after `delay`."""

    def __init__(self, decision: OwnerDecision | None = None, delay: float = 0.0) -> None:
        self.decision = decision
        self.delay = delay
        self.asked: list[dict[str, Any]] = []
        self.cancelled = 0
        self.info: list[str] = []
        self.ring_alerts: list[str] = []

    async def ask(
        self,
        call_sid: str,
        summary_tr: str,
        options: list[OwnerOption],
        timeout: float,
        **kwargs: Any,
    ) -> OwnerDecision:
        self.asked.append({"call_sid": call_sid, "summary_tr": summary_tr, **kwargs})
        try:
            if self.decision is None:
                await asyncio.sleep(3600)
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        assert self.decision is not None
        return self.decision

    async def send_info(self, text: str, *, silent: bool = False) -> bool:
        self.info.append(text)
        return True

    async def send_ring_alert(self, text: str) -> bool:
        self.ring_alerts.append(text)
        return True


class FakeBot:
    """Telegram bot for the real OwnerChannel: records sent messages and edits."""

    def __init__(self, fail_send: bool = False) -> None:
        self.sent: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self._next_id = 100
        self._fail_send = fail_send

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> SimpleNamespace:
        if self._fail_send:
            raise ConnectionError("telegram down")
        self._next_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "message_id": self._next_id, **kwargs})
        return SimpleNamespace(message_id=self._next_id)

    async def edit_message_text(self, text: str, **kwargs: Any) -> None:
        self.edits.append({"text": text, **kwargs})

    async def edit_message_reply_markup(self, **kwargs: Any) -> None:
        self.edits.append({"text": None, **kwargs})


def option_decision(option_id: str = "door") -> OwnerDecision:
    return OwnerDecision(DecisionKind.OPTION, option_id=option_id)
