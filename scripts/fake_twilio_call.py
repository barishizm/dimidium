"""Place a test call against the running app without Twilio: this script plays Twilio's part.

It posts a (signed) /twilio/voice webhook, opens the media-stream WebSocket the TwiML points
to, and talks like a caller: each --say line is synthesized with a second voice (or each --wav
file is played) once the agent has gone quiet. The agent's audio is recorded, and the script
prints how long the agent took to start answering each line, measured on the wire.

Usage (app running with `uvicorn app.main:app`):
    python scripts/fake_twilio_call.py \\
        --say "Laba diena, čia DPD kurjeris. Turiu jums siuntinį." \\
        --say "Ar galiu palikti prie durų?"

The webhook is signed with TWILIO_AUTH_TOKEN from .env (or --auth-token). Without a token, start
the app with TWILIO_VALIDATE_SIGNATURE=false. Output: out/fake_calls/<call>.wav (stereo: left =
caller, right = agent).
"""

import argparse
import asyncio
import base64
import json
import secrets
import sys
import time
import wave
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import numpy as np
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.speech.audio import load_audio_as_mulaw, mulaw_to_pcm16  # noqa: E402
from app.speech.base import VoiceConfig  # noqa: E402
from app.speech.factory import create_tts  # noqa: E402
from app.telephony.twilio_webhook import compute_signature  # noqa: E402

FRAME_BYTES = 160  # 20 ms of 8 kHz mu-law
FRAME_SECONDS = 0.02
SILENCE = b"\xff" * FRAME_BYTES
# Agent audio louder than this RMS (16-bit) counts as speech; hold music counts too.
SPEECH_RMS = 300
CALLER_VOICES = {"azure": "lt-LT-LeonasNeural", "google": "lt-LT-Chirp3-HD-Charon"}


@dataclass
class CallState:
    caller_frames: list[bytes] = field(default_factory=list)
    agent_frames: list[bytes] = field(default_factory=list)
    agent_last_voice: float = 0.0
    agent_spoke: bool = False
    clears: int = 0
    ended: asyncio.Event = field(default_factory=asyncio.Event)
    # Time each caller line finished, and when the agent was first heard after it.
    line_ends: list[float] = field(default_factory=list)
    replies: dict[int, float] = field(default_factory=dict)


async def caller_audio(args: argparse.Namespace) -> list[tuple[str, bytes]]:
    lines: list[tuple[str, bytes]] = [
        (Path(p).name, load_audio_as_mulaw(Path(p))) for p in args.wav
    ]
    if args.say:
        settings = get_settings()
        tts = create_tts(settings, args.caller_tts)
        voice = VoiceConfig(language=settings.caller_language, voice=CALLER_VOICES[args.caller_tts])
        try:
            for text in args.say:
                audio = b"".join([chunk async for chunk in tts.synthesize(text, voice)])
                lines.append((text, audio))
        finally:
            await tts.aclose()
    return lines


async def open_call(args: argparse.Namespace, call_sid: str) -> tuple[str, dict[str, str]]:
    settings = get_settings()
    form = {"CallSid": call_sid, "From": args.caller, "To": args.to, "CallStatus": "ringing"}
    headers = {}
    token = args.auth_token or (
        settings.twilio_auth_token.get_secret_value() if settings.twilio_auth_token else None
    )
    if token:
        # Sign the URL the app believes Twilio used (PUBLIC_BASE_URL), as Twilio would.
        signed_url = settings.public_base_url.rstrip("/") + "/twilio/voice"
        headers["X-Twilio-Signature"] = compute_signature(
            signed_url, {k: [v] for k, v in form.items()}, token
        )
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(f"{args.base_url}/twilio/voice", data=form, headers=headers)
    response.raise_for_status()
    stream = ET.fromstring(response.text).find("Connect/Stream")
    if stream is None:
        raise SystemExit(f"The app did not offer a media stream:\n{response.text}")
    params = {p.get("name", ""): p.get("value", "") for p in stream.findall("Parameter")}
    ws_url = args.base_url.replace("https://", "wss://").replace("http://", "ws://")
    return f"{ws_url}/twilio/stream", params


async def send_audio(
    ws: websockets.ClientConnection,
    stream_sid: str,
    state: CallState,
    lines: list[tuple[str, bytes]],
    gap: float,
    reply_timeout: float,
) -> None:
    pending = list(lines)
    current = b""
    started = time.monotonic()
    sent = 0
    while not state.ended.is_set():
        now = time.monotonic()
        if not current and pending and your_turn(state, now, gap, reply_timeout):
            label, current = pending.pop(0)
            print(f"[{now - started:6.2f}s] caller: {label}")
        if current:
            frame, current = current[:FRAME_BYTES], current[FRAME_BYTES:]
            frame = frame.ljust(FRAME_BYTES, b"\xff")
            if not current:
                state.line_ends.append(time.monotonic())
        else:
            frame = SILENCE
        state.caller_frames.append(frame)
        payload = base64.b64encode(frame).decode()
        try:
            await ws.send(
                json.dumps(
                    {"event": "media", "streamSid": stream_sid, "media": {"payload": payload}}
                )
            )
        except websockets.ConnectionClosed:
            break
        sent += 1
        await asyncio.sleep(max(0.0, started + sent * FRAME_SECONDS - time.monotonic()))


def your_turn(state: CallState, now: float, gap: float, reply_timeout: float) -> bool:
    """Speak once the agent has answered the previous line (or never does) and gone quiet."""
    quiet = now - state.agent_last_voice >= gap
    if not state.line_ends:
        return state.agent_spoke and quiet  # after the greeting
    last_end = state.line_ends[-1]
    answered = state.agent_last_voice > last_end
    return (answered and quiet) or now - last_end >= reply_timeout


async def receive_audio(ws: websockets.ClientConnection, state: CallState) -> None:
    try:
        async for raw in ws:
            message = json.loads(raw)
            if message.get("event") == "clear":
                state.clears += 1
                print("        (agent audio cleared: barge-in or end of hold)")
            if message.get("event") != "media":
                continue
            frame = base64.b64decode(message["media"]["payload"])
            state.agent_frames.append(frame)
            pcm = np.frombuffer(mulaw_to_pcm16(frame), dtype="<i2").astype(np.float64)
            if pcm.size and np.sqrt(np.mean(pcm**2)) > SPEECH_RMS:
                now = time.monotonic()
                state.agent_last_voice = now
                state.agent_spoke = True
                for index, end in enumerate(state.line_ends):
                    if index not in state.replies and now > end:
                        state.replies[index] = now - end
    except websockets.ConnectionClosed:
        pass
    finally:
        state.ended.set()


def save_recording(state: CallState, path: Path) -> None:
    caller = np.frombuffer(mulaw_to_pcm16(b"".join(state.caller_frames)), dtype="<i2")
    agent = np.frombuffer(mulaw_to_pcm16(b"".join(state.agent_frames)), dtype="<i2")
    length = max(len(caller), len(agent))
    stereo = np.zeros((length, 2), dtype="<i2")
    stereo[: len(caller), 0] = caller
    stereo[: len(agent), 1] = agent
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(stereo.tobytes())


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--say", action="append", default=[], help="caller line (synthesized)")
    parser.add_argument("--wav", action="append", default=[], help="caller line (audio file)")
    parser.add_argument("--caller-tts", choices=sorted(CALLER_VOICES), default="azure")
    parser.add_argument("--caller", default="+37060000000", help="caller number (From)")
    parser.add_argument("--to", default="+37052000000", help="called number (To)")
    parser.add_argument("--auth-token", default=None, help="defaults to TWILIO_AUTH_TOKEN")
    parser.add_argument("--gap", type=float, default=1.2, help="agent silence before speaking")
    parser.add_argument("--reply-timeout", type=float, default=15, help="max wait for a reply")
    parser.add_argument("--max-seconds", type=float, default=180)
    args = parser.parse_args()
    if not args.say and not args.wav:
        parser.error("give at least one --say or --wav")

    print("Preparing caller audio...")
    lines = await caller_audio(args)
    call_sid = "CA" + secrets.token_hex(16)
    stream_url, params = await open_call(args, call_sid)
    stream_sid = "MZ" + secrets.token_hex(16)
    state = CallState()
    print(f"Call {call_sid[-4:]} -> {stream_url}")

    async with websockets.connect(stream_url, max_size=None) as ws:
        await ws.send(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
        await ws.send(
            json.dumps(
                {
                    "event": "start",
                    "sequenceNumber": "1",
                    "streamSid": stream_sid,
                    "start": {
                        "streamSid": stream_sid,
                        "accountSid": "AC" + "0" * 32,
                        "callSid": call_sid,
                        "tracks": ["inbound"],
                        "customParameters": params,
                        "mediaFormat": {
                            "encoding": "audio/x-mulaw",
                            "sampleRate": 8000,
                            "channels": 1,
                        },
                    },
                }
            )
        )
        receiver = asyncio.create_task(receive_audio(ws, state))
        sender = asyncio.create_task(
            send_audio(ws, stream_sid, state, lines, args.gap, args.reply_timeout)
        )
        try:
            await asyncio.wait_for(state.ended.wait(), args.max_seconds)
            print("The app ended the call.")
        except TimeoutError:
            print(f"No hang-up after {args.max_seconds:.0f}s; hanging up.")
            await ws.send(json.dumps({"event": "stop", "streamSid": stream_sid}))
        sender.cancel()
        receiver.cancel()

    out = Path("out/fake_calls") / f"{call_sid[-8:]}.wav"
    save_recording(state, out)
    print("\nAgent started answering (end of caller line -> first agent audio, on the wire):")
    for index, (label, _) in enumerate(lines[: len(state.line_ends)]):
        reply = state.replies.get(index)
        timing = f"{reply * 1000:6.0f} ms" if reply is not None else "   no reply"
        print(f"  {timing}  {label[:60]}")
    print(f"\nRecording: {out} (left = caller, right = agent)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
