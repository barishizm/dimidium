"""A real LiveCall without telephony: GPT-Live over the primary WebSocket, audio from us.

The SIP path receives events on the sideband; the primary WebSocket carries the same events
plus the audio we stream in. So LiveCall runs here unchanged (functions, permission checks,
guardrail, hold, disclosure check), and only the caller is simulated: pre-rendered PCM
turns, paced in real time, with silence in between (GPT-Live expects a continuous stream).

Used by the adversarial tests (tests/test_adversarial.py, scripts/adversarial.py). Needs
OPENAI_API_KEY and costs about $0.05 per simulated minute plus backend tokens.
"""

import asyncio
import base64
import contextlib
import json
import logging
import time
from collections import deque
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI

from app.live.call import LiveCall
from app.live.transport import SidebandDisconnected, SidebandLink

logger = logging.getLogger(__name__)

SAMPLE_RATE = 24000
FRAME_MS = 20
FRAME_BYTES = SAMPLE_RATE * 2 * FRAME_MS // 1000
_SILENCE = bytes(FRAME_BYTES)


class WebSocketLink:
    """The primary connection as a SidebandLink; events read before LiveCall started (up to
    session.started) are replayed first. Every received event is also kept for the report."""

    def __init__(self, connection: Any, replay: list[dict[str, Any]]) -> None:
        self._connection = connection
        self._replay = deque(replay)
        self.events: list[dict[str, Any]] = list(replay)
        self.last_output_at = 0.0
        self.closed = False

    async def send(self, event: dict[str, Any]) -> None:
        if self.closed:
            raise SidebandDisconnected("connection closed")
        await self._connection.send(event)

    async def recv(self) -> dict[str, Any] | None:
        if self._replay:
            return self._replay.popleft()
        from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

        try:
            data = await self._connection.recv_bytes()
        except ConnectionClosedOK:
            self.closed = True
            return None
        except ConnectionClosedError as exc:
            self.closed = True
            raise SidebandDisconnected(str(exc)) from exc
        event = json.loads(data)
        if not isinstance(event, dict):
            return {}
        self.events.append(event)
        if event.get("type") in ("session.output_transcript.delta", "session.output_audio.delta"):
            self.last_output_at = time.monotonic()
        return event

    async def close(self) -> None:
        # The connection belongs to the runner; LiveCall closing its link must not drop it.
        return None


class OneLinkConnector:
    """attach() hands out the one connection; a re-attach is impossible on WebSocket."""

    def __init__(self, link: WebSocketLink) -> None:
        self._link: WebSocketLink | None = link

    async def attach(self, session_id: str) -> SidebandLink:
        link, self._link = self._link, None
        if link is None:
            raise SidebandDisconnected("the WebSocket cannot be re-attached")
        return link


class WebSocketControl:
    """Hangup = session.close on the same connection (accept/reject do not apply)."""

    def __init__(self, link: WebSocketLink) -> None:
        self._link = link

    async def accept(self, session_id: str, session: dict[str, Any]) -> None:
        return None

    async def reject(self, session_id: str, status_code: int) -> None:
        return None

    async def hangup(self, session_id: str) -> None:
        with contextlib.suppress(SidebandDisconnected):
            await self._link.send({"type": "session.close"})


class CallerAudio:
    """A continuous 20 ms PCM stream: queued caller turns, silence otherwise."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection
        self._frames: deque[bytes] = deque()
        self._drained = asyncio.Event()
        self._drained.set()
        self.stopped = False

    def say(self, pcm: bytes) -> None:
        for i in range(0, len(pcm), FRAME_BYTES):
            frame = pcm[i : i + FRAME_BYTES]
            self._frames.append(frame + bytes(FRAME_BYTES - len(frame)))
        self._drained.clear()

    async def wait_said(self) -> None:
        await self._drained.wait()

    async def run(self) -> None:
        start = time.monotonic()
        sent = 0
        while not self.stopped:
            frame = self._frames.popleft() if self._frames else _SILENCE
            if not self._frames:
                self._drained.set()
            try:
                await self._connection.send(
                    {
                        "type": "session.input_audio.append",
                        "audio": base64.b64encode(frame).decode(),
                    }
                )
            except Exception:
                return
            sent += 1
            delay = start + sent * FRAME_MS / 1000 - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)


@dataclass
class LocalHost:
    """CallHost for one local call: runs background work and lets the runner await it."""

    draining: bool = False
    tasks: list[asyncio.Task[None]] = field(default_factory=list)

    def spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        self.tasks.append(asyncio.create_task(coro))

    def track_call(self, call_sid: str, end_politely: Any) -> None:
        return None

    def untrack_call(self, call_sid: str) -> None:
        return None

    async def drain(self, timeout: float) -> None:
        if self.tasks:
            await asyncio.wait(self.tasks, timeout=timeout)


@dataclass(frozen=True)
class Pacing:
    """How the simulated caller waits for the agent (seconds)."""

    greeting_timeout: float = 15.0
    # The agent has finished a reply after this much output silence...
    quiet: float = 2.0
    # ...once it has started replying; a reply that never starts is given up after this.
    reply_timeout: float = 25.0
    # Hard cap per caller turn (an ask_owner hold counts against it).
    turn_cap: float = 60.0
    # After the last turn: time for delegations, functions and the goodbye.
    tail: float = 20.0
    closed_wait: float = 15.0


DEFAULT_PACING = Pacing()


async def wait_for_agent(
    link: WebSocketLink, call: LiveCall, since: float, pacing: Pacing, first_timeout: float
) -> None:
    """Return once the agent has replied after `since` and gone quiet (or timeouts pass).
    Waits through owner holds: a pending ask_owner is not a finished reply."""
    deadline = time.monotonic() + pacing.turn_cap
    start_deadline = time.monotonic() + first_timeout
    while time.monotonic() < deadline and not link.closed:
        started = link.last_output_at > since
        if not started and time.monotonic() >= start_deadline:
            return
        quiet = time.monotonic() - link.last_output_at >= pacing.quiet
        if started and quiet and not call.owner_pending:
            return
        await asyncio.sleep(0.1)


async def run_local_call(
    client: AsyncOpenAI,
    session_config: dict[str, Any],
    make_call: Any,
    caller_turns: list[bytes],
    pacing: Pacing = DEFAULT_PACING,
) -> tuple[LiveCall, WebSocketLink]:
    """Start a GPT-Live session, run LiveCall on it, speak `caller_turns` one by one.

    `make_call(session_id, connector, control, host)` builds the LiveCall (the caller of this
    function picks the owner channel, settings and timings). Returns the finished call and its
    link (all received events in `link.events`).
    """
    async with client.live.connect(max_retries=0) as connection:
        await connection.send({"type": "session.start", "session": session_config})
        replay: list[dict[str, Any]] = []
        session_id: str | None = None
        while session_id is None:
            event = json.loads(await connection.recv_bytes())
            replay.append(event)
            if event.get("type") == "session.started":
                session_id = str((event.get("session") or {}).get("id") or "local")
            elif event.get("type") == "error":
                raise RuntimeError(f"session.start refused: {event.get('error')}")

        link = WebSocketLink(connection, replay)
        host = LocalHost()
        call: LiveCall = make_call(session_id, OneLinkConnector(link), WebSocketControl(link), host)
        audio = CallerAudio(connection)
        audio_task = asyncio.create_task(audio.run())
        call_task = asyncio.create_task(call.run())
        try:
            # The greeting first, then one caller turn per agent reply.
            await wait_for_agent(link, call, 0.0, pacing, pacing.greeting_timeout)
            for pcm in caller_turns:
                if link.closed or call_task.done():
                    break
                audio.say(pcm)
                await audio.wait_said()
                await wait_for_agent(link, call, time.monotonic(), pacing, pacing.reply_timeout)
            tail_end = time.monotonic() + pacing.tail
            while time.monotonic() < tail_end and not call_task.done():
                if time.monotonic() - link.last_output_at > pacing.quiet * 2 and not (
                    call.owner_pending or call.functions_running
                ):
                    break
                await asyncio.sleep(0.2)
            if not call_task.done():
                await call.hang_up_now("local call finished")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(call_task), pacing.closed_wait)
        finally:
            audio.stopped = True
            for task in (audio_task, call_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(audio_task, call_task, return_exceptions=True)
            await host.drain(timeout=30)
    return call, link


async def synthesize(
    client: AsyncOpenAI, text: str, *, model: str, voice: str, instructions: str | None = None
) -> bytes:
    """Caller speech as raw 24 kHz mono PCM16 (the format the session expects)."""
    kwargs: dict[str, Any] = {"input": text, "model": model, "voice": voice}
    if instructions:
        kwargs["instructions"] = instructions
    response = await client.audio.speech.create(response_format="pcm", **kwargs)
    return response.content
