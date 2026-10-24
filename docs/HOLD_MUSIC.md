# Hold music on the GPT-Live path

Status: study, not implemented. Sources: guides/voice-server-controls ("Control playback when needed"), guides/live-delegation, and the `openai` SDK 3.36 (read 2026-10-24).

## Short answer

- **Direct SIP (our current path): not feasible.** Audio flows between Twilio and OpenAI only, and our app never touches it. The guide says playback is controlled "at the client or media relay", and "a sideband alone does not control playback". The sideband has no event for playing our own audio to the caller.
- **WebSocket relay path: feasible.** This is the fallback in [GPT_LIVE_DESIGN.md](GPT_LIVE_DESIGN.md) §1: Twilio Media Streams → our server → GPT-Live WebSocket. Our server owns every audio frame there, so it can mix in music while `ask_owner` is pending.

So on SIP the caller hears spoken hold lines instead: the hold announcement, then up to `HOLD_STILL_TRYING_MAX` "still trying" lines every `HOLD_PHRASE_INTERVAL_SECONDS`, sent as `session.commentary.append`. We have no `HOLD_MUSIC` setting, because on SIP it could not do anything.

## Options that were ruled out

| Option | Why not |
|---|---|
| `session.output_audio.fixture.play` (in the SDK, ≤30 s of 24 kHz PCM) | Not in any guide (*unverified*): payload, SIP support, and how it interacts with model speech are unknown. Building on it would mean relying on an undocumented API. |
| Twilio plays music (`<Play>`, conference hold, `<Enqueue>`) | The call would have to leave the SIP leg to OpenAI and come back. That breaks the single-call-leg rule in CLAUDE.md and drops the GPT-Live session context. |
| Ask the agent to hum or describe music | Not music, and it sounds wrong. |

## What it would take on the relay path

1. Build the relay first (Media Streams WebSocket ↔ GPT-Live WebSocket, `audio/pcmu` passthrough). It is a different transport, but the same session config and function flow (`app/live/config.py` already builds both).
2. While `ask_owner` is pending, mix a quiet loop into the frames we send to Twilio. The removed Pipecat pipeline had a generator and file loader for 8 kHz μ-law hold music (`app/telephony/hold_audio.py`, in git history before the cleanup) that could be restored.
3. When GPT-Live speaks (a still-trying line, or an answer to the caller on hold), duck or pause the music and resume after its audio ends. If an instruction interrupts speech, clear the queued audio first, as the guide requires. An instruction acknowledgment is not a signal to resume.
4. Keep the caller's microphone open (no `session.input_audio.mute`), so caller speech on hold is still transcribed and answered.
5. Gate it behind a relay-only flag (e.g. `LIVE_HOLD_MUSIC=false` by default).

Cost: an extra audio hop and our server's uptime becomes part of every call's audio. That is the same trade-off as the relay itself, so hold music alone is not a reason to switch paths.
