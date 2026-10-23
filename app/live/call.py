"""One accepted GPT-Live SIP call: the sideband event loop, from accept to session.closed.

GPT-Live talks to the caller by itself. This loop listens on the sideband and:
- stores transcripts (turns from input/output transcript deltas) in the CallSession and SQLite;
- checks that the AI + recording disclosure was actually spoken, and re-sends it if not;
- executes the backend model's function calls (validated, permission-checked in code) and
  returns their output (response.item.create + response.create);
- keeps the caller company while the owner is asked (commentary, never silence);
- enforces the call limits, and hangs up only after the goodbye has finished playing;
- on a sideband drop re-attaches once; on a backend failure retries once; otherwise apologizes,
  takes a message, tells the owner and ends the call.

Event and field names: openai SDK 3.36 (openai.types.live) and guides/voice-server-controls.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from app import db
from app.agent.guards import RISK_EVENTS, PrivateInfoGuard, detect_caller_risk
from app.agent.tools import (
    TOOL_INPUT_MODELS,
    AskOwnerInput,
    EndCallInput,
    FlagSuspiciousInput,
    SaveMessageInput,
)
from app.config import Settings
from app.live.transcript import TranscriptAssembler, disclosure_spoken
from app.live.transport import CallControl, SidebandConnector, SidebandDisconnected, SidebandLink
from app.models import CallSession, CallStatus, TranscriptTurn
from app.owner_channel import OwnerChannel, make_ask_owner_handler, short_call_id
from app.post_call import CallFacts, CallSummarizer, format_report, needs_attention, store_text
from app.speech.phrases_lt import PHRASES_BY_KEY
from app.telephony.call_handlers import CallRecorder

logger = logging.getLogger(__name__)

# Re-attach attempts after the sideband drops ("reconnect once").
MAX_RECONNECTS = 1
# Retries of a failed backend (Responses) turn before the fallback.
MAX_BACKEND_RETRIES = 1
# Timeline margin after the last reflected output audio before the goodbye counts as played.
_PLAYBACK_MARGIN_MS = 300


def _say(key: str) -> str:
    return PHRASES_BY_KEY[key].text


# Instructions and commentary for GPT-Live (English; it speaks the caller's language).
DISCLOSURE_INSTRUCTION = (
    "Before anything else, tell the caller now, exactly, in Lithuanian (in English if the "
    f'caller speaks English): "{_say("disclosure")}" Then continue the conversation.'
)
SOFT_WRAP_UP_INSTRUCTION = (
    "The call is almost at its time limit. Do not start new topics: get any missing detail, "
    "then say goodbye and end the call within the next 30 seconds."
)
WRAP_UP_INSTRUCTION = (
    "End the call now. Tell the caller, in one or two short sentences in their language: "
    f'"{_say("time_limit")} {_say("goodbye")}" Do not ask anything else.'
)
FALLBACK_INSTRUCTION = (
    "A technical problem occurred. Tell the caller now, in their language: "
    f'"{_say("technical_issue")}" If you do not know yet, ask for their name and why they are '
    "calling. Do not promise anything else. After they answer, thank them and say goodbye."
)
PRIVATE_INFO_INSTRUCTION = (
    "You just said a private detail of the owner. Stop. Do not repeat it or confirm it. Tell "
    f'the caller: "{_say("private_info")}" and continue.'
)
HOLD_COMMENTARY = (
    "The owner has been asked and the answer is pending. Tell the caller once, briefly: "
    f'"{_say("hold")}"'
)
STILL_WAITING_COMMENTARY = (
    "Still waiting for the owner's answer. Tell the caller briefly: " f'"{_say("still_trying")}"'
)

OK_OUTPUT = "OK"
MESSAGE_ONLY_OUTPUT = (
    "ERROR: the owner cannot be asked on this call. Take a message with save_message, then say "
    "goodbye and call end_call."
)
LIMIT_OUTPUT = (
    "LIMIT: the owner was already asked the maximum number of times. Do not ask again. Use the "
    "decision you have or take a message with save_message, then say goodbye and call end_call."
)
FUNCTION_ERROR_OUTPUT = (
    "ERROR: the function failed. Apologize, offer to take a message, and do not promise anything."
)


def owner_timeout_output(hold_timeout: float) -> str:
    return (
        f"OWNER_TIMEOUT: the owner did not answer within {round(hold_timeout)} seconds. "
        "Apologize, take a message with save_message, then say goodbye and call end_call."
    )


# session.closed reasons -> Turkish end reason for the owner's report.
_CLOSE_REASONS_TR = {
    "remote_hangup": "arayan kapattı",
    "close_requested": "asistan kapattı",
    "expired": "oturum süresi doldu",
    "content": "güvenlik filtresi görüşmeyi kesti",
    "connection_lost": "bağlantı koptu",
}


class CallHost(Protocol):
    """What a call needs from the app (TelephonyDeps implements it)."""

    def spawn(self, coro: Coroutine[Any, Any, None]) -> None: ...

    def track_call(self, call_sid: str, end_politely: Callable[[], Awaitable[None]]) -> None: ...

    def untrack_call(self, call_sid: str) -> None: ...


@dataclass(frozen=True)
class LiveCallTimings:
    """All waits of a call, in seconds (tests shrink them)."""

    disclosure_deadline: float = 10.0
    goodbye_quiet: float = 1.5
    goodbye_max: float = 10.0
    fallback_grace: float = 30.0
    # After end_call: how long to wait for the goodbye to start before checking for quiet.
    goodbye_start_timeout: float = 4.0
    # On hold: speak the hold phrase ourselves if the agent said nothing this long.
    hold_announce_after: float = 2.5
    hold_phrase_interval: float = 20.0
    reconnect_delay: float = 0.5
    # A client event waits this long for a re-attached sideband before it is dropped.
    send_wait: float = 5.0
    # After our hangup request: wait this long for session.closed.
    closed_wait: float = 10.0
    # save_message / flag_suspicious side effects (Telegram, SQLite).
    function_timeout: float = 15.0
    # Extra time over HOLD_TIMEOUT_SECONDS for the Telegram round trips of ask_owner.
    ask_margin: float = 20.0
    poll: float = 0.1

    @classmethod
    def from_settings(cls, settings: Settings) -> "LiveCallTimings":
        return cls(
            disclosure_deadline=settings.live_disclosure_deadline_seconds,
            goodbye_quiet=settings.live_goodbye_quiet_seconds,
            goodbye_max=settings.live_goodbye_max_seconds,
            fallback_grace=settings.live_fallback_grace_seconds,
            hold_phrase_interval=settings.hold_phrase_interval_seconds,
        )


class LiveCall:
    def __init__(
        self,
        session: CallSession,
        *,
        settings: Settings,
        connector: SidebandConnector,
        control: CallControl,
        host: CallHost,
        owner_channel: OwnerChannel | None,
        summarizer: CallSummarizer | None = None,
        timings: LiveCallTimings | None = None,
    ) -> None:
        if session.live_session_id is None:
            raise ValueError("LiveCall needs session.live_session_id")
        self._session = session
        self._session_id: str = session.live_session_id
        self._settings = settings
        self._connector = connector
        self._control = control
        self._host = host
        self._channel = owner_channel
        self._summarizer = summarizer
        self._t = timings or LiveCallTimings.from_settings(settings)
        self._recorder = CallRecorder(settings.db_path, session)
        self._ask = (
            make_ask_owner_handler(owner_channel, session, settings.hold_timeout_seconds)
            if owner_channel is not None
            else None
        )
        self._guard = PrivateInfoGuard.build(
            settings.private_term_list,
            [settings.owner_forward_number, settings.twilio_phone_number],
        )

        self._link: SidebandLink | None = None
        self._link_ready = asyncio.Event()
        self._closed = asyncio.Event()
        self._done = False
        self._tasks: set[asyncio.Task[None]] = set()
        self._seen_events: set[str] = set()
        self._seen_calls: set[str] = set()
        self._assembler = TranscriptAssembler()

        self._started = 0.0
        # Loop time of the last agent speech (transcript or reflected audio).
        self._last_output_at = float("-inf")
        self._agent_spoke = asyncio.Event()
        # Session timeline: end of the last reflected output audio, and an anchor that maps
        # loop time to timeline ms (from real-time events: caller transcript, appended acks).
        self._last_output_end_ms: int | None = None
        self._anchor: tuple[float, int] | None = None

        self._disclosure_text = ""
        self.disclosure_verified = False
        self.disclosure_resent = False

        self._owner_questions = 0
        self._pending_question: AskOwnerInput | None = None
        self._heard_on_hold: list[str] | None = None
        self._backend_retries = 0
        self.failure: str | None = None
        self._ending = False
        self._hung_up = False
        self._end_reason: str | None = None
        self.close_reason: str | None = None
        self.usage_seconds: float | None = None
        # For the end-of-call report.
        self.flagged = False
        self.messages_left = 0
        self.hung_up_on_hold = False

    # -- lifecycle ------------------------------------------------------------------------

    @property
    def call_sid(self) -> str:
        return self._session.call_sid

    @property
    def tag(self) -> str:
        return f"#{short_call_id(self.call_sid)} · {self._session.caller_number}"

    def _log(self, **extra: Any) -> dict[str, Any]:
        return {"call_sid": self.call_sid, "live_session_id": self._session_id, **extra}

    def _now(self) -> float:
        return asyncio.get_running_loop().time()

    async def run(self) -> None:
        """Run the call until the session closes; then store and report it (in the background)."""
        self._started = self._now()
        self._session.status = CallStatus.IN_PROGRESS
        await self._recorder.flush()
        self._host.track_call(self.call_sid, self.end_for_shutdown)
        try:
            self._spawn(self._enforce_duration())
            self._spawn(self._ensure_disclosure())
            reconnects = 0
            while True:
                drop = await self._attach_and_pump()
                if drop is None or self._hung_up:
                    break
                if reconnects >= MAX_RECONNECTS:
                    await self._sideband_lost(drop)
                    break
                reconnects += 1
                logger.warning("Sideband dropped (%s); re-attaching", drop, extra=self._log())
                await asyncio.sleep(self._t.reconnect_delay)
        except Exception:
            self._session.status = CallStatus.FAILED
            logger.exception("Live call loop crashed", extra=self._log())
            with suppress(Exception):
                await self._hang_up("sistem hatası")
        finally:
            self._done = True
            self._host.untrack_call(self.call_sid)
            await self._cancel_tasks()
            last = self._assembler.flush()
            if last is not None:
                self._session.transcript.append(last)
            if self._session.status is not CallStatus.FAILED:
                self._session.status = CallStatus.COMPLETED
            self._session.ended_at = datetime.now(UTC)
            self._host.spawn(self.finish())

    async def _attach_and_pump(self) -> str | None:
        """Attach and handle events. None once the session is closed, else why it dropped."""
        try:
            link = await self._connector.attach(self._session_id)
        except Exception as exc:
            return f"attach failed: {exc}"
        self._link = link
        self._link_ready.set()
        logger.info("Sideband attached", extra=self._log())
        try:
            while not self._closed.is_set():
                event = await link.recv()
                if event is None:
                    return None if self._closed.is_set() else "closed without session.closed"
                try:
                    await self._handle(event)
                except Exception:
                    logger.exception("Could not handle %s", event.get("type"), extra=self._log())
            return None
        except SidebandDisconnected as exc:
            return str(exc) or "disconnected"
        finally:
            self._link = None
            self._link_ready.clear()
            with suppress(Exception):
                await link.close()

    async def end_for_shutdown(self) -> None:
        """Server shutdown: say goodbye and hang up (TelephonyDeps.end_active_calls)."""
        self._spawn(self._finish_with_goodbye("sunucu yeniden başlatılıyor", WRAP_UP_INSTRUCTION))

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        if self._done:
            coro.close()
            return
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("Call task failed", exc_info=task.exception(), extra=self._log())

    async def _cancel_tasks(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # -- sending --------------------------------------------------------------------------

    async def _send(self, event: dict[str, Any]) -> bool:
        """Send a client event; waits briefly for a re-attach. False if it could not be sent."""
        if self._link is None and not self._done:
            with suppress(TimeoutError):
                await asyncio.wait_for(self._link_ready.wait(), self._t.send_wait)
        link = self._link
        if link is None:
            logger.warning("Dropped %s: no sideband", event.get("type"), extra=self._log())
            return False
        try:
            await link.send(event)
        except Exception as exc:
            logger.warning("Could not send %s: %s", event.get("type"), exc, extra=self._log())
            return False
        return True

    async def _instruct(self, content: str) -> bool:
        # delegation_id must be null with Responses delegation.
        return await self._send(
            {"type": "session.instructions.append", "delegation_id": None, "content": content}
        )

    async def _commentary(self, content: str) -> bool:
        return await self._send(
            {"type": "session.commentary.append", "delegation_id": None, "content": content}
        )

    async def _thinking(self, content: str) -> bool:
        return await self._send(
            {"type": "session.thinking.append", "delegation_id": None, "content": content}
        )

    # -- events ---------------------------------------------------------------------------

    async def _handle(self, event: dict[str, Any]) -> None:
        kind = event.get("type", "")
        event_id = event.get("event_id")
        if isinstance(event_id, str):
            # A re-attach replays the last seconds of events.
            if event_id in self._seen_events:
                return
            self._seen_events.add(event_id)

        if kind == "session.input_transcript.delta":
            self._set_anchor(event.get("end_ms"))
            await self._on_transcript("caller", str(event.get("delta", "")))
        elif kind == "session.output_transcript.delta":
            self._mark_output()
            await self._on_transcript("agent", str(event.get("delta", "")))
        elif kind == "session.output_audio.delta":
            self._mark_output()
            end_ms = event.get("end_ms")
            if isinstance(end_ms, int):
                self._last_output_end_ms = max(self._last_output_end_ms or 0, end_ms)
        elif kind in (
            "session.instructions.appended",
            "session.commentary.appended",
            "session.thinking.appended",
        ):
            self._set_anchor(event.get("end_ms"))
        elif kind == "session.delegation.created":
            delegation = event.get("delegation") or {}
            logger.info(
                "Delegation %s to %s",
                delegation.get("id"),
                delegation.get("target"),
                extra=self._log(),
            )
        elif kind == "response.event":
            await self._on_backend_event(event.get("event") or {})
        elif kind == "session.usage.updated":
            self.usage_seconds = (event.get("usage") or {}).get("seconds", self.usage_seconds)
        elif kind == "error":
            await self._on_error(event.get("error") or {})
        elif kind == "session.closed":
            self.close_reason = str(event.get("reason", ""))
            self.usage_seconds = (event.get("usage") or {}).get("seconds", self.usage_seconds)
            logger.info(
                "Session closed: %s", self.close_reason, extra=self._log(usage=self.usage_seconds)
            )
            self._closed.set()

    def _mark_output(self) -> None:
        self._last_output_at = self._now()
        self._agent_spoke.set()

    def _set_anchor(self, end_ms: object) -> None:
        if isinstance(end_ms, int):
            self._anchor = (self._now(), end_ms)

    def _timeline_ms(self) -> float | None:
        """Estimated current position on the session timeline."""
        if self._anchor is None:
            return None
        at, ms = self._anchor
        return ms + (self._now() - at) * 1000

    async def _on_transcript(self, speaker: str, delta: str) -> None:
        if not delta:
            return
        if speaker == "agent" and not self.disclosure_verified:
            self._disclosure_text += delta
            if disclosure_spoken(self._disclosure_text):
                self.disclosure_verified = True
                logger.info("AI + recording disclosure spoken", extra=self._log())
        turn = self._assembler.add("caller" if speaker == "caller" else "agent", delta)
        if turn is not None:
            await self._on_turn(turn)

    async def _on_turn(self, turn: TranscriptTurn) -> None:
        self._session.transcript.append(turn)
        if turn.speaker == "caller":
            if self._heard_on_hold is not None:
                self._heard_on_hold.append(turn.text)
            for risk in detect_caller_risk(turn.text):
                # Silent context for the agent; the backend prompt decides what to do.
                await self._thinking(RISK_EVENTS[risk])
        elif self._guard.leaks(turn.text):
            # Too late to stop the audio (no pre-playback check on SIP): stop the repeat, alert.
            logger.warning("Agent speech contained a private detail", extra=self._log())
            await self._instruct(PRIVATE_INFO_INSTRUCTION)
            self.flagged = True
            await self._alert("Asistan özel bir bilgiyi söylemiş olabilir (gizli bilgi filtresi).")
        await self._recorder.flush()

    async def _on_error(self, error: dict[str, Any]) -> None:
        code = error.get("code")
        logger.warning(
            "Live error %s: %s",
            code,
            error.get("message"),
            extra=self._log(param=error.get("param")),
        )
        if code == "responses_validation_failed":
            # The backend configuration was refused: no function will ever run on this call.
            await self._fail("backend configuration rejected")

    async def _on_backend_event(self, nested: dict[str, Any]) -> None:
        kind = nested.get("type", "")
        if kind == "response.output_item.done":
            item = nested.get("item") or {}
            if item.get("type") == "function_call":
                self._start_function(item)
        elif kind == "response.completed":
            self._backend_retries = 0
        elif kind in ("response.failed", "error"):
            await self._on_backend_failure(nested)
        elif kind == "response.incomplete":
            logger.warning("Backend response incomplete", extra=self._log())

    async def _on_backend_failure(self, nested: dict[str, Any]) -> None:
        detail = (nested.get("response") or {}).get("error") or nested.get("error") or nested
        logger.error("Backend model failed: %s", str(detail)[:300], extra=self._log())
        if self.failure is not None or self._ending:
            return
        if self._backend_retries < MAX_BACKEND_RETRIES:
            self._backend_retries += 1
            await self._send({"type": "response.create"})
            return
        await self._fail("backend model failed twice")

    # -- functions ------------------------------------------------------------------------

    def _start_function(self, item: dict[str, Any]) -> None:
        call_id = item.get("call_id")
        if not isinstance(call_id, str) or not call_id or call_id in self._seen_calls:
            return
        self._seen_calls.add(call_id)
        name, raw = str(item.get("name", "")), str(item.get("arguments") or "")
        # A task: transcripts keep flowing while the owner decides.
        self._spawn(self._run_function(call_id, name, raw))

    async def _run_function(self, call_id: str, name: str, raw: str) -> None:
        logger.info("Function call %s", name, extra=self._log(function_call_id=call_id))
        try:
            output = await self.execute(name, raw)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Function %s failed", name, extra=self._log())
            output = FUNCTION_ERROR_OUTPUT
        sent_at = self._now()
        await self._send(
            {
                "type": "response.item.create",
                "item": {"type": "function_call_output", "call_id": call_id, "output": output},
            }
        )
        await self._send({"type": "response.create"})
        if name == "end_call" and output == OK_OUTPUT:
            await self._finish_with_goodbye(self._end_reason or "asistan bitirdi", None, sent_at)

    async def execute(self, name: str, raw_arguments: str) -> str:
        """Validate and run one function call. The output is what the backend model sees."""
        model = TOOL_INPUT_MODELS.get(name)
        if model is None:
            logger.warning("Unknown function %r", name, extra=self._log())
            return f"ERROR: unknown function {name!r}."
        try:
            args: BaseModel = model.model_validate_json(raw_arguments or "{}")
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or 'arguments'}: {err['msg']}"
                for err in exc.errors(include_url=False)
            )
            logger.warning("Invalid %s arguments: %s", name, problems, extra=self._log())
            return f"ERROR: invalid arguments: {problems}"

        if isinstance(args, AskOwnerInput):
            return await self._ask_owner(args)
        if isinstance(args, EndCallInput):
            self._end_reason = f"asistan bitirdi: {args.reason}"
            return OK_OUTPUT
        if isinstance(args, SaveMessageInput):
            await asyncio.wait_for(self._save_message(args), self._t.function_timeout)
            return OK_OUTPUT
        if isinstance(args, FlagSuspiciousInput):
            logger.warning("Suspicious call flagged", extra=self._log())
            self.flagged = True
            await asyncio.wait_for(
                self._alert(f"Şüpheli arama: {args.reason}"), self._t.function_timeout
            )
            return OK_OUTPUT
        return f"ERROR: unknown function {name!r}."

    async def _ask_owner(self, args: AskOwnerInput) -> str:
        if self._session.message_only:
            return MESSAGE_ONLY_OUTPUT
        if self._owner_questions >= self._settings.max_owner_questions:
            logger.warning("ask_owner over the per-call limit; wrapping up", extra=self._log())
            self._spawn(self._finish_with_goodbye("soru sınırı aşıldı", WRAP_UP_INSTRUCTION))
            return LIMIT_OUTPUT
        self._owner_questions += 1
        if self._ask is None:
            logger.warning(
                "ask_owner without an owner channel; taking a message", extra=self._log()
            )
            return owner_timeout_output(self._settings.hold_timeout_seconds)

        self._session.collected_info["owner_question"] = args.summary_tr
        self._session.status = CallStatus.ON_HOLD
        self._pending_question = args
        self._heard_on_hold = []
        company = asyncio.create_task(self._keep_company(self._now()))
        try:
            result = await asyncio.wait_for(
                self._ask(args), self._settings.hold_timeout_seconds + self._t.ask_margin
            )
        except TimeoutError:
            logger.warning("ask_owner did not finish in time", extra=self._log())
            return owner_timeout_output(self._settings.hold_timeout_seconds)
        else:
            self._pending_question = None
            if result.timed_out:
                return owner_timeout_output(self._settings.hold_timeout_seconds)
            self._session.owner_decision = result.decision_text_en
            heard = list(self._heard_on_hold or [])
            partial = self._assembler.partial("caller")
            if partial:
                heard.append(partial)
            output = f"OWNER_DECISION: {result.decision_text_en}"
            if heard:
                output += "\nCALLER_SAID_DURING_HOLD: " + " / ".join(heard)
            return output
        finally:
            company.cancel()
            with suppress(asyncio.CancelledError):
                await company
            self._heard_on_hold = None
            if self._session.status is CallStatus.ON_HOLD:
                self._session.status = CallStatus.IN_PROGRESS
            if not self._done:
                # Still set only when the call ended during the hold (see finish()).
                self._pending_question = None

    async def _keep_company(self, since: float) -> None:
        """Never silence on hold: announce the hold if the agent did not, then repeat."""
        await asyncio.sleep(self._t.hold_announce_after)
        if self._last_output_at < since:
            await self._commentary(HOLD_COMMENTARY)
        while True:
            await asyncio.sleep(self._t.hold_phrase_interval)
            await self._commentary(STILL_WAITING_COMMENTARY)

    async def _save_message(self, args: SaveMessageInput) -> None:
        details = "\n".join(
            f"{key}: {value}"
            for key, value in args.model_dump(exclude_none=True).items()
            if key != "purpose"
        )
        await self.save_note(args.purpose, details)

    @property
    def _push(self) -> OwnerChannel | None:
        """The channel, unless this call must stay quiet (answer mode "off")."""
        return self._channel if self._session.notify_owner else None

    async def save_note(self, purpose: str, details: str) -> None:
        text = f"{purpose}\n{details}".strip()
        self.messages_left += 1
        delivered = False
        if self._push is not None:
            delivered = await self._push.send_info(f"📝 Mesaj {self.tag}\n{text}")
        try:
            await db.add_owner_message(self._settings.db_path, self.call_sid, text, delivered)
        except Exception:
            logger.exception("Could not store the owner message", extra=self._log())

    async def _alert(self, text: str) -> None:
        if self._push is not None:
            await self._push.send_info(f"⚠️ {self.tag}\n{text}")

    # -- disclosure, limits, ending -------------------------------------------------------

    async def _wait_until(self, condition: Callable[[], bool], timeout: float) -> bool:
        deadline = self._now() + timeout
        while not condition():
            if self._now() >= deadline:
                return False
            await asyncio.sleep(self._t.poll)
        return True

    async def _ensure_disclosure(self) -> None:
        """The greeting is a startup developer message, which does not guarantee the words.
        Check the agent's transcript; if the disclosure is missing, have it said right away."""
        deadline = self._t.disclosure_deadline
        await self._wait_until(self._agent_spoke.is_set, deadline)
        if await self._wait_until(lambda: self.disclosure_verified, deadline):
            return
        logger.warning(
            "AI + recording disclosure not found in the agent's speech; sending it now",
            extra=self._log(),
        )
        # Only what is said from now on counts.
        self._disclosure_text = ""
        self.disclosure_resent = True
        await self._instruct(DISCLOSURE_INSTRUCTION)
        if not await self._wait_until(lambda: self.disclosure_verified, deadline):
            logger.error("Disclosure still not confirmed after re-sending", extra=self._log())

    async def _enforce_duration(self) -> None:
        settings = self._settings
        soft_at = self._started + settings.max_call_seconds - settings.wrap_up_seconds
        if settings.wrap_up_seconds > 0 and soft_at > self._started:
            await asyncio.sleep(max(0.0, soft_at - self._now()))
            if not self._ending:
                await self._instruct(SOFT_WRAP_UP_INSTRUCTION)
        await asyncio.sleep(max(0.0, self._started + settings.max_call_seconds - self._now()))
        logger.warning("Call reached MAX_CALL_SECONDS", extra=self._log())
        await self._finish_with_goodbye("süre sınırı", WRAP_UP_INSTRUCTION)

    async def _fail(self, reason: str) -> None:
        """Unrecoverable backend failure: apologize, take a message, tell the owner, end."""
        if self.failure is not None or self._ending:
            return
        self.failure = reason
        logger.error("Call falls back to taking a message: %s", reason, extra=self._log())
        self._spawn(self._fallback())

    async def _fallback(self) -> None:
        await self._instruct(FALLBACK_INSTRUCTION)
        await self._alert(
            "Teknik sorun: asistan karar veremiyor. Arayandan mesaj alınıyor, arama kısa süre "
            "içinde kapanacak."
        )
        await asyncio.sleep(self._t.fallback_grace)
        await self._finish_with_goodbye("teknik sorun", WRAP_UP_INSTRUCTION)

    async def _sideband_lost(self, drop: str) -> None:
        """Re-attaching failed: nothing can be said or executed any more. End the call."""
        logger.error("Sideband lost for good (%s); hanging up", drop, extra=self._log())
        self.failure = self.failure or "sideband lost"
        await self._alert(
            "Bağlantı sorunu: arama kontrol edilemedi ve kapatıldı. Konuşulanlar aşağıda."
        )
        await self._hang_up("bağlantı koptu", wait_for_close=False)

    async def _finish_with_goodbye(
        self, reason: str, instruction: str | None, since: float | None = None
    ) -> None:
        """Optionally tell the agent to say goodbye, wait until it has been spoken, hang up."""
        if self._ending:
            return
        self._ending = True
        since = self._now() if since is None else since
        if instruction is not None:
            await self._instruct(instruction)
        await self._wait_for_goodbye(since)
        await self._hang_up(reason)

    async def _wait_for_goodbye(self, since: float) -> None:
        """No event says "playback finished": wait for the agent's speech to start, then for
        it to stop (no output for goodbye_quiet) and for the timeline to pass the end of the
        last reflected output audio. Capped at goodbye_max."""
        start = self._now()
        deadline = start + self._t.goodbye_max
        await self._wait_until(
            lambda: self._last_output_at > since,
            min(self._t.goodbye_start_timeout, self._t.goodbye_max),
        )

        def played() -> bool:
            if self._now() - self._last_output_at < self._t.goodbye_quiet:
                return False
            position = self._timeline_ms()
            if position is None or self._last_output_end_ms is None:
                return True
            return position >= self._last_output_end_ms + _PLAYBACK_MARGIN_MS

        if not await self._wait_until(played, max(0.0, deadline - self._now())):
            logger.warning("Goodbye wait capped; hanging up", extra=self._log())

    async def _hang_up(self, reason: str, *, wait_for_close: bool = True) -> None:
        if self._hung_up:
            return
        self._hung_up = True
        self._ending = True
        self._end_reason = reason
        logger.info("Hanging up: %s", reason, extra=self._log())
        try:
            await self._control.hangup(self._session_id)
        except Exception:
            logger.exception("Hangup request failed", extra=self._log())
        if wait_for_close:
            self._spawn(self._expect_close())

    async def _expect_close(self) -> None:
        """Keep the sideband open for session.closed (final usage), but not forever."""
        if await self._wait_until(self._closed.is_set, self._t.closed_wait):
            return
        logger.warning("No session.closed after hangup", extra=self._log())
        self._closed.set()
        link = self._link
        if link is not None:
            with suppress(Exception):
                await link.close()

    # -- after the call -------------------------------------------------------------------

    def end_reason(self) -> str:
        if self._hung_up and self._end_reason:
            return self._end_reason
        if self.close_reason:
            return _CLOSE_REASONS_TR.get(self.close_reason, self.close_reason)
        return self._end_reason or "bilinmiyor"

    async def finish(self) -> None:
        """After the call: store everything, report missing pieces, summary, Telegram report."""
        await self._recorder.flush()
        if self._pending_question is not None:
            self.hung_up_on_hold = True
            if self._push is not None:
                text = (
                    "📵 Arayan beklerken kapattı"
                    if self.close_reason == "remote_hangup"
                    else ("📵 Arama beklerken sona erdi")
                )
                await self._push.send_info(
                    f"{text} {self.tag}\n{self._pending_question.summary_tr}\n"
                    f'🗣 "{self._pending_question.caller_said_lt}"'
                )
        if self.failure is not None:
            said = [t.text for t in self._session.transcript if t.speaker == "caller"]
            await self.save_note(
                "Teknik sorun sırasında arayanın söyledikleri",
                "\n".join(f'"{line}"' for line in said) or "(arayan bir şey söylemedi)",
            )
        if self.close_reason == "content":
            self.flagged = True
            await self._alert("Görüşme OpenAI güvenlik filtresi tarafından kesildi.")
        if not self.disclosure_verified and any(
            t.speaker == "agent" for t in self._session.transcript
        ):
            await self._alert("Açılış bildirimi (yapay zekâ + kayıt) konuşmada doğrulanamadı.")

        end_reason = self.end_reason()
        logger.info(
            "Call finished",
            extra=self._log(end_reason=end_reason, usage_seconds=self.usage_seconds),
        )
        facts = CallFacts(
            end_reason=end_reason,
            owner_decision=self._session.owner_decision,
            messages_left=self.messages_left,
            flagged=self.flagged,
            message_only=self._session.message_only,
            hung_up_on_hold=self.hung_up_on_hold,
        )
        summary = (
            await self._summarizer.summarize(self._session, facts) if self._summarizer else None
        )
        try:
            await db.set_call_outcome(
                self._settings.db_path,
                self.call_sid,
                summary=store_text(summary),
                end_reason=end_reason,
                follow_up=summary.needs_follow_up if summary else bool(facts.messages_left),
                flagged=facts.flagged,
            )
        except Exception:
            logger.exception("Could not store the call outcome", extra=self._log())
        if self._push is not None:
            text = format_report(self.tag, self._session, facts, summary)
            await self._push.send_info(text, silent=not needs_attention(facts, summary))
