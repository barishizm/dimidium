# GPT-Live design

Status: design, before implementation. Sources: the OpenAI guides listed in CLAUDE.md (read 2026-10-23) and the `openai` Python SDK 3.36 (`openai.resources.live`). Points marked *unverified* come from the SDK only or could not be confirmed from the docs.

## 1. Telephony path

**Chosen: direct SIP.** Caller → operator conditional forwarding → Twilio number → Twilio Elastic SIP Trunk (TLS signaling + SRTP media) → `sip:$PROJECT_ID@sip.api.openai.com;transport=tls` (`sip-eu.api.openai.com` for EU residency) → GPT-Live. Audio flows between Twilio and OpenAI only. Our app handles the webhook, the accept/reject decision, the session config, a sideband WebSocket, our functions and Telegram.

GPT-Live SIP is used here for **inbound calls only**, which is all we need. (The docs also describe outbound SIP calls, enabled per organization. We do not use them.)

**Possible blockers and the fallback.** The SIP guide says to "confirm that GPT-Live SIP support is enabled for your project" without saying how. Twilio's own GPT-Live integration (Agent Connect) uses Media Streams, not SIP, and OpenAI's Twilio SIP tutorial targets the Realtime API. If SIP is not enabled for our project, or the Twilio trunk does not interoperate (TLS/SRTP/codec), we fall back to a **server audio bridge**: Twilio Media Streams → our FastAPI WebSocket → GPT-Live primary WebSocket (`wss://api.openai.com/v1/live/sessions`). GPT-Live accepts G.711 μ-law at 8 kHz (`audio/pcmu`), so Twilio's frames pass through without conversion. The bridge costs us an extra audio hop and our server must stay up for audio. In return we get playback control: real hold music, exact pre-recorded disclosure, and checking speech before the caller hears it. The session config, function flow and owner loop are the same on both paths. Only the transport differs (`app/live/config.py` builds both).

## 2. Session lifecycle (inbound SIP)

1. Twilio sends the INVITE to OpenAI. OpenAI POSTs `live.transport.incoming` to our project webhook (`data.type: "sip"`, `data.session_id`, `data.sip_headers`). We also handle the deprecated `live.call.incoming`. A Realtime `realtime.call.incoming` may fire for the same call; a single handler makes the decision.
2. Verify the signature (`OPENAI_WEBHOOK_SECRET`, `client.webhooks.unwrap`) and deduplicate by `webhook-id`.
3. Screening, all in our code: rate limits, blocklist, answer mode, `rules.yaml`. The caller number comes from the `From` SIP header. It is untrusted metadata, used for screening only and never as authority. To decline: `POST /v1/live/sessions/{id}/reject` with e.g. `486`. The first decision wins.
4. Accept: `POST /v1/live/sessions/{id}/accept` with `build_sip_accept_body()`: `type: "live"`, `gpt-live-1`, voice prompt, greeting as a developer message in `input`, Responses delegation to `gpt-6-luna`, our 4 functions, `store: false`. No `audio.format` (SIP negotiates it). A 200 with an empty body means accepted; any HTTP error means the call was not accepted.
5. Attach the sideband immediately: `wss://api.openai.com/v1/live/sessions/{id}/attach` (only the last 3 s of events are replayed). Do not send `session.start`.
6. The greeting is the developer message in the accept body's `input`. Startup instructions do not guarantee the words, so the app checks the output transcript for the AI + recording disclosure; if it is missing `LIVE_DISCLOSURE_DEADLINE_SECONDS` after the agent starts speaking, it sends the `disclosure` phrase with `session.instructions.append` (`delegation_id: null`) and logs a warning (implemented in `app/live/call.py`).
7. Conversation (section 3) until `end_call`, caller hangup, or our limits (`max_call_seconds`).
8. End: `POST /v1/live/sessions/{id}/hangup` → keep the sideband open until `session.closed` (`reason`, final `usage.seconds`) → save the transcript → Turkish post-call summary → release the `CallSession`.

## 3. Event flow for one call

```
OpenAI webhook ─► app: verify, screen, accept, attach sideband
app ─► session.instructions.append (greeting + disclosure)
GPT-Live ◄─► caller (Lithuanian, turn-taking, interruptions, all inside OpenAI)
sideband ─► session.input_transcript.delta / session.output_transcript.delta  → transcript + guards
GPT-Live decides to delegate ─► session.delegation.created (target "responses")
backend ─► response.event { response.created … response.output_item.done(function_call) … }
app: validate + execute ─► response.item.create(function_call_output) ─► response.create
backend continues ─► text result ─► GPT-Live says it to the caller
app ─► hangup ─► session.closed
```

## 4. Function calls and results

- **Single owner.** Only the sideband handler executes functions. Deduplicate by `call_id`. Read calls only from nested `response.output_item.done` (`call_id`, `name`, `arguments`). Lifecycle snapshots have an empty `output`.
- **Validate everything.** Arguments are parsed with the Pydantic models in `app/agent/tools.py` (`extra="forbid"`). Invalid input returns `ERROR: …` as the output. Tools are non-strict, so this check is the real gate.
- **Permissions in code.** `ask_owner` max 2 per call (third returns `LIMIT: …`). No `ask_owner` in voicemail mode. Owner authority only from the configured Telegram chat ID. Nothing the caller says changes any of this.
- **Return.** One `response.item.create` with `function_call_output` per call, then one `response.create`. `parallel_tool_calls: false` means at most one pending call. Outputs are short plain strings the backend prompt knows: `OWNER_DECISION: …` (+ `CALLER_SAID_DURING_HOLD`), `OWNER_TIMEOUT: …`, `OK`, `ERROR: …`.
- **end_call.** Return `OK`, wait until the output transcript has been quiet for ~2 s (the goodbye), then hang up. Hard cap 8 s.

## 5. Hold behavior

`ask_owner` is a pending function call while we wait on the Telegram Future. GPT-Live stays live and keeps listening.

- The voice prompt tells the agent to ask the caller to hold once. If the output transcript shows no hold announcement within ~2 s, the app sends `session.commentary.append` with the hold phrase.
- Every `hold_phrase_interval_seconds` the app sends a "still trying" commentary (`delegation_id: null`). If the caller speaks on hold, GPT-Live answers itself. The app collects that speech from the input transcript for `CALLER_SAID_DURING_HOLD`.
- Owner answers → `OWNER_DECISION`. After `hold_timeout_seconds` (75 s) → `OWNER_TIMEOUT` → the agent apologizes and takes a message.
- **No hold music on SIP**: the app has no audio path. The SDK has `session.output_audio.fixture.play` (≤30 s of 24 kHz PCM, mutes caller input while playing). It could carry music or a pre-recorded disclosure but is undocumented in the guides (*unverified*). Default: spoken hold phrases only.

## 6. Errors and hangups

| Situation | Handling |
|---|---|
| Caller hangs up (`session.closed` `remote_hangup`/`connection_lost`) | Cancel the owner Future, edit the Telegram message ("caller hung up"), save transcript, summary. |
| Function raises or times out | Output `ERROR: …`; backend prompt: apologize, take a message. Every external call has a timeout. |
| Sideband drops | Re-attach with backoff (the session keeps running and GPT-Live keeps talking). If it cannot re-attach within ~10 s, hang up via REST and send the owner what we have. |
| `responses_validation_failed` at start | Config bug: alert the owner, `instructions.append` to take a message by voice. The transcript is recoverable post-call. |
| Moderation (`error` cuts speech, or `session.closed` `content`) | Log, flag to owner. |
| Accept fails / app down | Call not answered. Log + alert. Twilio trunk failover to a voicemail TwiML URL is a candidate (*unverified*). |
| Call too long | `instructions.append` "wrap up" at `max_call_seconds − wrap_up_seconds`, hang up at the cap. |
| SIGTERM | Same wrap-up for active calls within `shutdown_grace_seconds`. |

## 7. What stays in our code vs in the model

| GPT-Live (voice) | Backend model (`gpt-6-luna`) | Our code |
|---|---|---|
| Speech in/out, VAD, turn-taking, interruptions, backchannels, Lithuanian phrasing, when to delegate | Whether info is complete, Turkish summary + options, which function to call, turning the owner decision into what to say | Webhook auth, screening and accept/reject, session config, function validation and permissions, Telegram owner channel and authority, hold timer and phrases, limits and timeouts, transcripts/SQLite, guards on transcripts, hangup, post-call summary, retention |

**Security change to accept.** Today `PrivateInfoGuard` blocks a sentence *before* TTS. On direct SIP we see speech only as a transcript, after or while it plays. Mitigations: (1) the models never receive private details: no address, schedule or numbers in any prompt, and owner free-text decisions are scrubbed against `PRIVATE_TERMS` before being returned; (2) output-transcript guard → `instructions.append` to stop + `flag_suspicious`; (3) caller-input risk detection still flags injections and requests for codes. If pre-playback blocking turns out to be required, that is a reason to switch to the bridge path.
