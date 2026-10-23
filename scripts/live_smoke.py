"""Open one GPT-Live session over WebSocket with our config and play a caller recording into it.

Validates the OpenAI key, the session configuration and the event flow before any telephony
work: prints caller/agent transcripts, delegations, backend (Responses) events and function
calls. Function calls are validated like the app will validate them and answered locally; the
owner's Telegram answer is simulated with --owner-reply (or --owner-timeout).

Usage:
  python scripts/live_smoke.py --print-config            # no API call, no key needed
  python scripts/live_smoke.py --audio caller.wav [--owner-reply "Leave it at the door."]
  python scripts/live_smoke.py --audio caller.wav --owner-timeout --save-output agent.wav

Needs OPENAI_API_KEY in .env and the audio extras: pip install -e ".[bench]".
The recording is streamed in real time (20 ms frames), then silence for --tail seconds; the
session ends on end_call, after the tail, or on Ctrl+C.
"""

import argparse
import asyncio
import base64
import json
import signal
import sys
import time
import wave
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import ValidationError  # noqa: E402

from app.agent.tools import TOOL_INPUT_MODELS  # noqa: E402
from app.config import Settings, get_settings  # noqa: E402
from app.live.config import build_session_config, resolve_greeting  # noqa: E402
from app.live.prompts import greeting_instruction  # noqa: E402
from app.rules import RulesEngine  # noqa: E402

SAMPLE_RATE = 24000
FRAME_MS = 20
FRAME_BYTES = SAMPLE_RATE * 2 * FRAME_MS // 1000
CLOSE_TIMEOUT_SECONDS = 15
# Time for the goodbye to be spoken after end_call before the session is closed.
GOODBYE_GRACE_SECONDS = 5

OWNER_TIMEOUT_RESULT = (
    "OWNER_TIMEOUT: the owner did not answer. Apologize, take a message with "
    "save_message, then say goodbye and call end_call."
)


def load_pcm24k(path: Path) -> bytes:
    """Any soundfile-readable file -> raw mono PCM16 little-endian at 24 kHz."""
    import numpy as np
    import soundfile
    import soxr

    samples, rate = soundfile.read(str(path), dtype="float32", always_2d=True)
    mono = samples.mean(axis=1)
    if rate != SAMPLE_RATE:
        mono = soxr.resample(mono, rate, SAMPLE_RATE, quality="HQ")
    return (np.clip(mono, -1.0, 1.0) * 32767).astype("<i2").tobytes()


class SmokeCall:
    def __init__(self, connection: Any, settings: Settings, args: argparse.Namespace) -> None:
        self._conn = connection
        self._settings = settings
        self._args = args
        self._owner_questions = 0
        self._speaker: str | None = None
        self._output_audio = bytearray()
        self._usage_seconds: float | None = None
        self._close_requested = asyncio.Event()
        self.finalized = False
        self.errors = 0

    # -- output ---------------------------------------------------------------------------

    def _line(self, text: str) -> None:
        if self._speaker is not None:
            print(flush=True)
            self._speaker = None
        print(text, flush=True)

    def _transcript(self, speaker: str, delta: str) -> None:
        if speaker != self._speaker:
            if self._speaker is not None:
                print(flush=True)
            print(f"{speaker}: ", end="", flush=True)
            self._speaker = speaker
        print(delta, end="", flush=True)

    # -- sending --------------------------------------------------------------------------

    async def stream_audio(self, pcm: bytes) -> None:
        """Send the recording paced in real time, then silence until the session closes."""
        silence = bytes(FRAME_BYTES)
        tail_frames = int(self._args.tail * 1000 / FRAME_MS)
        frames = [pcm[i : i + FRAME_BYTES] for i in range(0, len(pcm), FRAME_BYTES)]
        start = time.monotonic()
        sent = 0
        while not self._close_requested.is_set():
            if sent < len(frames):
                frame = frames[sent]
                frame += bytes(FRAME_BYTES - len(frame))
            elif sent < len(frames) + tail_frames:
                frame = silence
            else:
                self._line("-- tail finished, closing")
                self.request_close()
                return
            await self._conn.session.input_audio.append(audio=base64.b64encode(frame).decode())
            sent += 1
            # Absolute schedule so the stream does not drift behind real time.
            delay = start + sent * FRAME_MS / 1000 - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)

    def request_close(self) -> None:
        self._close_requested.set()

    async def close_when_requested(self) -> None:
        await self._close_requested.wait()
        await self._conn.session.close(event_id="smoke_close")
        await asyncio.sleep(CLOSE_TIMEOUT_SECONDS)
        if not self.finalized:
            self._line(f"!! no session.closed within {CLOSE_TIMEOUT_SECONDS} s")
            await self._conn.close()

    # -- events ---------------------------------------------------------------------------

    async def handle(self, event: dict[str, Any]) -> None:
        kind = event.get("type", "")
        if kind == "session.started":
            self._line(f"-- session.started id={event['session'].get('id')}")
            greeting = greeting_instruction(resolve_greeting(self._settings))
            await self._conn.session.instructions.append(
                event_id="smoke_greeting", delegation_id=None, content=greeting
            )
        elif kind == "session.input_transcript.delta":
            self._transcript("CALLER", event.get("delta", ""))
        elif kind == "session.output_transcript.delta":
            self._transcript("AGENT", event.get("delta", ""))
        elif kind == "session.output_audio.delta":
            if self._args.save_output:
                self._output_audio += base64.b64decode(event["delta"])
        elif kind == "session.delegation.created":
            delegation = event.get("delegation", {})
            self._line(
                f"-- delegation id={delegation.get('id')} target={delegation.get('target')} "
                f"response_id={event.get('response_id') or delegation.get('response_id')}"
            )
        elif kind == "response.event":
            await self._handle_backend(event.get("delegation_id"), event.get("event", {}))
        elif kind == "session.usage.updated":
            self._usage_seconds = event.get("usage", {}).get("seconds")
        elif kind == "error":
            self.errors += 1
            err = event.get("error", {})
            self._line(
                f"!! error code={err.get('code')} type={err.get('type')} "
                f"client_event_id={err.get('client_event_id')} message={err.get('message')}"
            )
        elif kind == "session.closed":
            self.finalized = True
            usage = event.get("usage", {})
            self._line(f"-- session.closed reason={event.get('reason')} usage={usage}")
        elif self._args.verbose:
            self._line(f"-- {kind}")

    async def _handle_backend(self, delegation_id: str | None, nested: dict[str, Any]) -> None:
        kind = nested.get("type", "")
        if kind == "response.output_item.done":
            item = nested.get("item", {})
            if item.get("type") == "function_call":
                # Run as a task so transcripts keep flowing while the "owner" decides.
                asyncio.create_task(self._run_function(item))
            elif item.get("type") == "message":
                text = " ".join(
                    part.get("text", "") for part in item.get("content", []) if part.get("text")
                )
                self._line(f"-- backend text: {text}")
        elif kind == "response.completed":
            usage = nested.get("response", {}).get("usage")
            self._line(f"-- backend response.completed delegation={delegation_id} usage={usage}")
        elif kind in ("response.failed", "response.incomplete", "error"):
            self.errors += 1
            self._line(f"!! backend {kind}: {json.dumps(nested, ensure_ascii=False)[:500]}")
        elif self._args.verbose:
            self._line(f"-- backend {kind}")

    async def _run_function(self, item: dict[str, Any]) -> None:
        name, call_id, raw = item.get("name"), item.get("call_id"), item.get("arguments", "")
        self._line(f">> function_call {name} call_id={call_id} arguments={raw}")
        output = await self._execute(str(name), raw)
        self._line(f"<< function_call_output {call_id}: {output}")
        await self._conn.response.item.create(
            event_id=f"out_{call_id}",
            item={"type": "function_call_output", "call_id": call_id, "output": output},
        )
        await self._conn.response.create(event_id=f"continue_{call_id}")
        if name == "end_call":
            await asyncio.sleep(GOODBYE_GRACE_SECONDS)
            self.request_close()

    async def _execute(self, name: str, raw_arguments: str) -> str:
        """Validate like the app will, then simulate the side effect."""
        model = TOOL_INPUT_MODELS.get(name)
        if model is None:
            return f"ERROR: unknown function {name!r}."
        try:
            model.model_validate_json(raw_arguments or "{}")
        except ValidationError as exc:
            return f"ERROR: invalid arguments: {exc.errors(include_url=False)}"
        if name != "ask_owner":
            return "OK"
        if self._owner_questions >= self._settings.max_owner_questions:
            return (
                "LIMIT: the owner was already asked. Use the decision you have, or take a "
                "message with save_message."
            )
        self._owner_questions += 1
        await asyncio.sleep(self._args.owner_delay)
        if self._args.owner_timeout:
            return OWNER_TIMEOUT_RESULT
        return f"OWNER_DECISION: {self._args.owner_reply}"

    def save_output(self) -> None:
        if not self._args.save_output:
            return
        with wave.open(str(self._args.save_output), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(bytes(self._output_audio))
        self._line(f"-- agent audio saved to {self._args.save_output}")


async def run(args: argparse.Namespace, settings: Settings, session: dict[str, Any]) -> int:
    from openai import AsyncOpenAI

    pcm = load_pcm24k(args.audio)
    print(f"-- audio {args.audio}: {len(pcm) / (SAMPLE_RATE * 2):.1f} s", flush=True)
    assert settings.openai_api_key is not None
    client = AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value(), max_retries=0)
    async with client.live.connect(max_retries=0) as connection:
        call = SmokeCall(connection, settings, args)
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, call.request_close)
        await connection.session.start(session=session, event_id="smoke_start")  # type: ignore[arg-type]
        tasks: list[asyncio.Task[None]] = [asyncio.create_task(call.close_when_requested())]
        try:
            async for event in connection:
                payload = event.to_dict()
                if payload.get("type") == "session.started":
                    tasks.append(asyncio.create_task(call.stream_audio(pcm)))
                await call.handle(payload)
                if call.finalized:
                    break
        finally:
            loop.remove_signal_handler(signal.SIGINT)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            call.save_output()
    if not call.finalized:
        print("!! connection closed without session.closed (final usage unconfirmed)")
        return 1
    return 1 if call.errors else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--audio", type=Path, help="caller recording (wav, flac, ogg, mp3...)")
    parser.add_argument("--caller-number", default=None, help="E.164 number for rules.yaml")
    parser.add_argument(
        "--owner-reply",
        default="Tell the courier to leave the parcel at the door.",
        help="simulated owner decision returned to ask_owner",
    )
    parser.add_argument("--owner-timeout", action="store_true", help="simulate no owner answer")
    parser.add_argument("--owner-delay", type=float, default=5.0, help="seconds on hold")
    parser.add_argument("--tail", type=float, default=30.0, help="silence after the recording")
    parser.add_argument("--save-output", type=Path, help="write the agent's audio to this WAV")
    parser.add_argument("--print-config", action="store_true", help="print the session and exit")
    parser.add_argument("--verbose", action="store_true", help="print every event type")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = get_settings()
    rules_summary = RulesEngine.from_yaml(settings.rules_path).summary_for_call(args.caller_number)
    session = build_session_config(settings, rules_summary, "websocket")
    if args.print_config:
        print(json.dumps(session, ensure_ascii=False, indent=2))
        return 0
    if args.audio is None:
        print("FAIL: --audio is required (or use --print-config)")
        return 2
    if settings.openai_api_key is None:
        print("FAIL: OPENAI_API_KEY is empty in .env")
        return 1
    try:
        return asyncio.run(run(args, settings, session))
    except Exception as exc:  # noqa: BLE001 - report any connection/auth failure plainly
        print(f"FAIL: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
