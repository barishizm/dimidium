# Status audit

> **Note (2026-10-24):** this audit is a snapshot from before the switch from Twilio to Telnyx. Its Twilio findings (P0-1, P0-4, A2, A3, A17, the `TWILIO_*` rows) are superseded by [SETUP_TELNYX.md](SETUP_TELNYX.md); `scripts/check_sip.py` and `docs/SETUP_SIP.md` no longer exist. Left unedited as a record.

Date: 2026-10-24. Commit audited: `f052c6d` (main, clean tree). No code was changed for this audit.

Scope: every file under `app/`, `scripts/`, `tests/`, `docs/`, plus `CLAUDE.md`, `README.md`,
`.env.example`, deploy configs, the installed `openai` SDK 3.36.1 (`openai/types/live`), and
four OpenAI guides fetched today (voice-sip, voice-server-controls, live-delegation,
live-prompting).

---

## 1. What exists and works

### Test evidence

Run with the project venv (`.venv/bin/…`, Python 3.12.13). `ruff`, `black` and `pytest` are not
on the global PATH; activate the venv first.

```
$ ruff check .
All checks passed!

$ black --check .
would reformat tests/adversarial_cases.py
Oh no! 💥 💔 💥
1 file would be reformatted, 55 files would be left unchanged.

$ pytest -q
291 passed in 13.31s

$ python scripts/doctor.py --offline
env TWILIO_PHONE_NUMBER     FAIL    missing (see .env.example)
env RULES_PATH              WARN    rules.yaml not found: empty rules
env PRIVATE_TERMS           WARN    empty: no code check for your address or number
8 PASS, 2 WARN, 1 FAIL
```

The black failure is cosmetic. Black wants to join a split string literal at
`tests/adversarial_cases.py:174-175` onto one line.

**All tests run against fakes.** No test has called the real OpenAI API, and no real phone call
has been made.

### Built and covered by tests

| Area | Where | Tests |
|---|---|---|
| OpenAI webhook: signature check, rate limits per IP and global, body cap, dedupe by `webhook-id` and `session_id`, `live.transport.incoming` plus deprecated `live.call.incoming`, `realtime.call.incoming` ignored | `app/live/webhook.py` | `test_live_webhook.py`, `test_rate_limit.py` |
| Screening: forwarding loop, denylist, draining, concurrent-call cap, per-caller rate limit, allowlist ring alert, answer modes, voicemail | `app/live/sip.py`, `app/rules.py`, `app/answer_mode.py` | `test_live_webhook.py`, `test_rules.py` |
| Accept body: `type: live`, voice prompt, greeting as a developer `input` message, Responses delegation, our 4 function tools only, `store: false` | `app/live/config.py`, `app/live/prompts.py` | `test_live_config.py` |
| Sideband loop: transcripts, delegation events, nested `response.event`, function calls deduped by `call_id`, outputs grouped per response, one `response.create` not resent | `app/live/call.py`, `app/live/transport.py` | `test_live_call.py` (892 lines) |
| Strict argument validation (`extra="forbid"`, length caps), one retry then stop, unknown functions refused | `app/functions.py`, `LiveCall.execute` | `test_enforcement.py`, `test_functions.py` |
| `ask_owner`: cap (`MAX_OWNER_QUESTIONS`), no parallel asks, voicemail refusal, hold company (thinking, commentary, "still trying"), 75 s timeout, caller hang-up on hold, decision scrubbed of private terms | `app/live/call.py`, `app/owner_channel.py` | `test_live_call.py`, `test_owner_channel.py` |
| Telegram: owner chat ID checked on every callback and message, buttons, free text, timeout edit, `/calls /messages /mode /rules /status` | `app/owner_channel.py`, `app/owner_commands.py` | `test_owner_channel.py`, `test_owner_commands.py` |
| Guards: caller-risk detection, transcript guardrail (codes and payment, owner info), function-argument blocking, private-info leak check on agent speech | `app/guards.py`, `app/live/guardrail.py` | `test_guards.py`, `test_guardrail.py` |
| Disclosure check on the output transcript, re-sent if missing, audited in SQLite | `LiveCall._ensure_disclosure`, `app/db.py` | `test_live_call.py` |
| Limits: soft wrap-up, hard `MAX_CALL_SECONDS`, goodbye-aware hangup, backend retry once then fallback, sideband re-attach once then hangup | `app/live/call.py` | `test_live_call.py` |
| SQLite call log, post-call Turkish summary (Responses, Anthropic alternative), retention | `app/db.py`, `app/post_call.py`, `app/retention.py` | `test_post_call.py` |
| Polite shutdown (draining, goodbye on active calls), `/health` | `app/serve.py`, `app/host.py`, `app/main.py` | `test_shutdown.py`, `test_health.py`, `test_concurrency.py` |
| Secret masking in logs | `app/logging_setup.py` | `test_logging_setup.py` |
| Setup checks | `scripts/doctor.py`, `scripts/check_sip.py`, `scripts/check_telegram.py`, `scripts/live_smoke.py` | manual |
| Deploy | `Dockerfile` (`python -m app.serve`), `docker-compose.yml`, `fly.toml`, `render.yaml` | never built (Docker is not installed locally) |

### Configured in `.env` (names only)

Set: `OPENAI_API_KEY`, `OPENAI_WEBHOOK_SECRET`, `OPENAI_PROJECT_ID`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_OWNER_CHAT_ID`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `PUBLIC_BASE_URL` (ngrok
static domain).
Missing: `TWILIO_PHONE_NUMBER`, `rules.yaml`, `PRIVATE_TERMS`.

---

## 2. Stubbed, missing or inconsistent

### Stubbed or missing

| # | Item | Evidence |
|---|---|---|
| S1 | **Voice and backend prompts are drafts.** They are waiting for the owner's "section A1 / A2" texts. | `app/live/prompts.py:10-12` (`TODO(owner): DRAFT`) |
| S2 | **No Lithuanian phrase has been reviewed by a native speaker**, including the legal disclosure greeting. | every `Phrase` in `app/phrases_lt.py` has `reviewed=False`; `docs/REVIEW_LT.md` |
| S3 | **No runner for the adversarial cases.** The `local.py` docstring names `tests/test_adversarial.py` and `scripts/adversarial.py`. Neither file exists, and nothing imports `run_local_call` or `tests/adversarial_cases.py`. | `app/live/local.py:8`; CLAUDE.md "TODO after migration" |
| S4 | **No real-API run is on record.** `scripts/live_smoke.py` exists, but no test or log shows a GPT-Live session completing with our config. | no integration tests; `pytest` marker `integration` unused |
| S5 | **No `rules.yaml`.** The app runs with empty rules (no allowlist, denylist or known contacts). | `doctor --offline` WARN |
| S6 | **`PRIVATE_TERMS` is empty.** `PrivateInfoGuard` only knows the owner's numbers (`OWNER_PHONE_NUMBER` is not set either). | `doctor --offline` WARN; `.env` |
| S7 | **No handling for the `tool_outputs_required` / `function_call_outputs_required` errors** that the live-delegation guide names for missing outputs. Only `responses_validation_failed` is acted on. | `LiveCall._on_error`, `app/live/call.py:636-648` |
| S8 | Hold music: not possible on SIP as built. This is a study only. | `docs/HOLD_MUSIC.md` |

### Inconsistent with CLAUDE.md or docs/GPT_LIVE_DESIGN.md

| # | Design says | Code does | Where |
|---|---|---|---|
| I1 | CLAUDE.md: `ask_owner` returns `"OWNER_DECISION: ..."` or `"OWNER_TIMEOUT"`. | Returns JSON with `status` (`decided`, `timeout`, …). The design doc §4 picked JSON. CLAUDE.md was not updated. | `app/live/call.py:112-189` |
| I2 | CLAUDE.md: ask_owner is capped at 2 per call. | `MAX_OWNER_QUESTIONS` defaults to 2 but has no upper bound (`ge=1` only), so `.env` can raise it. | `app/config.py:112` |
| I3 | Design §3 diagram: the app sends `session.instructions.append` with the greeting and disclosure. `greeting_instruction` docstring: it is also sent "after session.started". | SIP only puts the greeting in the accept body's `input`. No append is sent unless the disclosure check fails. | `docs/GPT_LIVE_DESIGN.md:39`, `app/live/prompts.py:130-136` |
| I4 | Design §6: re-attach the sideband with backoff; give up after about 10 s. | Re-attaches once after 0.5 s (`MAX_RECONNECTS = 1`). | `app/live/call.py:51`, `:383-392` |
| I5 | Design §4: a third `ask_owner` returns `limit_reached` "and the call is wrapped up". | Both happen at once. `LIMIT_OUTPUT` tells the backend to take a message with `save_message`, while a `WRAP_UP_INSTRUCTION` ("End the call now. Do not ask anything else.") is sent in parallel. The two instructions contradict each other. | `app/live/call.py:828-831` |
| I6 | Design status line: "design, before implementation". | It is implemented. | `docs/GPT_LIVE_DESIGN.md:3` |
| I7 | Design §6 "Security change": "Today `PrivateInfoGuard` blocks a sentence before TTS". | That was the old pipeline. On SIP it checks after playback. | `docs/GPT_LIVE_DESIGN.md:78` |
| I8 | live-prompting guide: the model may paraphrase the greeting. For exact wording, use "application-controlled playback". | We rely on a transcript keyword check and a re-send. The disclosure can be heard late, or paraphrased in a way that still passes the keyword check. | `app/live/transcript.py`, `LiveCall._ensure_disclosure` |

### Dead code and stale comments

- `app/guards.py:4,28,38-49`: `sanitize_for_speech` ("no markdown … reach TTS"). Used only by
  `tests/test_guards.py`. GPT-Live speaks, so we have no TTS input to sanitize.
- `app/post_call.py:204,228-229`: the `latency` parameter of `format_report`. No caller passes it
  (it was a pipeline metric).
- `app/live/call.py:452`: the docstring names `TelephonyDeps.end_active_calls`. The real name is
  `AppHost.end_active_calls`.
- `app/session_registry.py:1`: "keyed by Twilio call SID". The key is now our `live-…` ID.
- `.env` (local, not committed) still holds removed settings, including **live secrets**:
  `STT_PROVIDER`, `TTS_PROVIDER`, `CALLER_LANGUAGE`, `FALLBACK_LANGUAGE`,
  `GOOGLE_APPLICATION_CREDENTIALS`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_STT_LOCATION`,
  `AZURE_SPEECH_KEY`, `AZURE_SPEECH_REGION`, `ANTHROPIC_MODEL`, `ANTHROPIC_EFFORT`,
  `ANTHROPIC_MAX_TOKENS`, `ANTHROPIC_TIMEOUT_SECONDS`, `ANTHROPIC_REFUSAL_FALLBACK`. Settings
  ignores them (`extra="ignore"`).

---

## 3. Mentions of Twilio, Pipecat, STT/TTS providers or Claude agent loops

Searched: `twilio|pipecat|media stream|stt|tts|speech-to-text|deepgram|elevenlabs|azure|google|claude|anthropic|agent loop|telephony|twiml|realtime`.
No hits for Pipecat (except one doc line), Deepgram, ElevenLabs, Google STT or a Claude agent
loop in `app/`.

**Legend:** OK means the mention is correct for the current architecture. **STALE** means
leftover from the removed pipeline, to fix or delete. ALT means the Claude summarizer, which
CLAUDE.md allows to stay as an alternative.

### Stale (fix)

| File:line | Mention |
|---|---|
| `app/guards.py:4` | "no markdown, emojis or URLs reach TTS" (`sanitize_for_speech`) |
| `app/guards.py:28` | "Symbols TTS would read out literally" |
| `app/session_registry.py:1` | "keyed by Twilio call SID" |
| `app/live/call.py:452` | `TelephonyDeps.end_active_calls` |
| `app/post_call.py:204,228-229` | `latency` (old pipeline metric) |
| `docs/GPT_LIVE_DESIGN.md:78` | "blocks a sentence *before* TTS" |
| `docs/HOLD_MUSIC.md:23` | "The removed Pipecat pipeline had a generator … (`app/telephony/hold_audio…`)". The path no longer exists. Fine as history; mark it as removed code. |
| `.env` (local) | `STT_PROVIDER`, `TTS_PROVIDER`, `GOOGLE_*`, `AZURE_SPEECH_*`, `ANTHROPIC_MODEL/EFFORT/MAX_TOKENS/TIMEOUT_SECONDS/REFUSAL_FALLBACK` |

### Twilio as SIP trunk carrier (OK)

- `app/config.py:83-86,125`: `TWILIO_*` settings, secret masking
- `app/live/sip.py:4`, `app/live/webhook.py:229`, `app/live/call.py:305`: own number used for loop detection and the private-number guard
- `.env.example:62,88-94`; `render.yaml:37-41`; `Dockerfile:5`
- `README.md:11,17,48,64,136,148,150,205,268,270-272`
- `docs/SETUP.md:10,25-58,75,95,101,138-148,194-209`
- `docs/SETUP_SIP.md:1,6-7,13,25-26,91-139,160,186,194,198`
- `docs/PRIVACY.md:16,23,67-68`
- `docs/GPT_LIVE_DESIGN.md:7,11,15,66`: line 11 discusses Media Streams as the fallback, line 66 TwiML failover (*unverified*)
- `docs/HOLD_MUSIC.md:7,8,17,22`: Media Streams relay as an option, Twilio `<Play>` rejected
- `scripts/check_sip.py:9,15-16,37,90,151-202,214`; `scripts/doctor.py:12,35,73-75,162-199,247`
- `tests/test_live_webhook.py:78,101,274`; `tests/test_logging_setup.py:31-32,64`

### Claude / Anthropic (ALT: optional post-call summarizer)

- `app/config.py:14,45-46,50,121`; `app/post_call.py:8,140-188`; `app/logging_setup.py:19` (key mask)
- `pyproject.toml:25-27` (`[anthropic]` extra); `.env.example:35-41`; `README.md:118`
- `tests/test_post_call.py:12,112-126`
- `README.md:7,191` refer to CLAUDE.md and Claude Code, not to the runtime.

### "realtime" / "TTS" that are fine

- `pyproject.toml:17` `openai[realtime]`: the extra that pulls in websockets for `client.live`.
- `app/live/webhook.py:5`, `docs/SETUP_SIP.md:76`, `tests/test_live_webhook.py:262`: `realtime.call.incoming` is deliberately ignored.
- `app/live/local.py:267-275`: OpenAI TTS (`audio.speech`) synthesizes the simulated caller in the local test harness only.

---

## 4. GPT-Live API assumptions not verified against current docs

Checked today against the guides, with doc quotes:

- `live.transport.incoming`, `data.type: "sip"`, `data.session_id`. Deprecated `live.call.incoming` has no `data.type`.
- accept, reject and hangup paths, `{status_code: 486}`, top-level `session`, 200 with an empty body
- sideband `wss://…/v1/live/sessions/{id}/attach`, do not send `session.start`
- `response.event` envelope, calls from nested `response.output_item.done` (`call_id`, `name`, `arguments`), `function_call_output` via `response.item.create` then `response.create`, do not resend `response.create`
- commentary is spoken, thinking is quiet, instructions can interrupt speech, `delegation_id: null`, 500 tokens per append
- no transcript-done event
- hosted tools only when listed in `delegation.responses.tools`
- `Generate <|SpawnThinking|>` and the three policy headings
- greeting as a developer message in `session.input`

Field names and enums also match the installed SDK 3.36.1 types.

**Still unverified (SDK-only, undocumented, or never exercised against the real API):**

| # | Assumption | Source today | Where it matters |
|---|---|---|---|
| A1 | GPT-Live SIP is enabled for our project. The guide only says "confirm that it is enabled" and gives no steps. | doc gap | the whole SIP path |
| A2 | Twilio Elastic SIP Trunk (TLS + SRTP) interoperates with `sip.api.openai.com` (codec, TLS, SRTP). OpenAI's Twilio SIP tutorial targets the Realtime API. Twilio's own GPT-Live integration uses Media Streams. | design §1 | call setup |
| A3 | The SIP header list is ordered, `P-Asserted-Identity` and `From` are present, and Twilio passes the original caller's number after GSM forwarding. | assumption | screening, caller ID |
| A4 | The sideband replays "only the last 3 s" of events. The guide does not specify any replay window. If attach is slow, early greeting transcript may be lost, and the disclosure is then re-sent. | design §2 | disclosure check |
| A5 | Event fields we read: `end_ms` on `session.input_transcript.delta` and on `*.appended` acks; `end_ms` on reflected `session.output_audio.delta`; `session.usage.updated.usage.seconds`; `session.closed.reason` and `usage`; `error.code == "responses_validation_failed"`. Present in SDK types, not in the guides. | SDK | goodbye timing, usage, failure handling |
| A6 | Reflected output audio is actually sent on the sideband for SIP sessions, so `_last_output_end_ms` gets set. If it is not, goodbye timing falls back to "quiet for 1.5 s". | SDK docstring | hangup cutting off the goodbye |
| A7 | The nested `response.created` carries `response.id`, and `output_item.done` has none. We map calls to the "current response per delegation". The SDK's `session.delegation.created` also has `response_id`, which is unused. | design §4 | grouping outputs |
| A8 | `delegation.responses` accepts `parallel_tool_calls: false`, `tool_choice: "auto"`, `reasoning.effort` (the SDK enum includes `xhigh`; whether `gpt-6-luna` supports each value is unknown), and `service_tier` (the SDK enum adds `fast_tier_temp_pilot` and `ultrafast`; ours is narrower). | SDK | accept body |
| A9 | Non-strict function tools with JSON Schema `maxLength`, `pattern`, `minItems` are accepted by the backend. | SDK | accept body |
| A10 | `voice: "marin"` is a valid built-in GPT-Live voice. Lithuanian quality with `gpt-live-1` is untested. | config default | caller experience |
| A11 | `store: false` is honored on SIP sessions, and no recording is kept by OpenAI. | design | GDPR, PRIVACY.md |
| A12 | GPT-Live keeps talking and listening while a function output is pending for 75+ s, with no backend timeout on a pending call. | design §5 | hold |
| A13 | `session.commentary.append` during a pending delegation is spoken promptly and does not conflict with the pending call. | guide (semantics only) | hold phrases |
| A14 | `session.output_audio.fixture.play` (≤30 s, 24 kHz PCM, sideband allowed per SDK) works on SIP. It would allow an exact pre-recorded disclosure. The SDK says `instructions.append` is rejected while a fixture plays. | SDK only | I8, hold music |
| A15 | After `POST …/hangup`, `session.closed` arrives on the sideband with `reason: "close_requested"`, followed by a clean close (`graceful_close=True`). | SDK | end-of-call bookkeeping |
| A16 | Moderation: an `error` cuts speech, or `session.closed` arrives with `reason: "content"`. | design §6 | flagging |
| A17 | Twilio trunk failover to a voicemail URL when our app is down. | design §6 (*unverified*) | app-down behavior |
| A18 | Model IDs `gpt-live-1` and `gpt-6-luna` are available to this key. `doctor.py` checks `/v1/models/{id}` online; it was not run online for this audit. | — | everything |

---

## 5. Prioritized fix list

### P0: blocks a real call

| # | Fix | Owner action? |
|---|---|---|
| P0-1 | **Get a Twilio number** (account funding, Lithuanian regulatory bundle or an EU alternative), then set `TWILIO_PHONE_NUMBER`. Blocked since 2026-10-22. | yes, onboarding step |
| P0-2 | **Confirm GPT-Live SIP is enabled for the OpenAI project** (A1), and run `scripts/doctor.py` online to confirm both model IDs (A18). If SIP is not available, the fallback is the Media Streams bridge (design §1), which is a large build. | yes (OpenAI support or dashboard) |
| P0-3 | **First real-API smoke run** with `scripts/live_smoke.py`: proves the accept/session config, delegation, function round trip and Lithuanian speech on the real models (S4, A5, A7-A10, A12). Cheap and needs no Twilio. | key already set; needs a caller recording |
| P0-4 | **Twilio Elastic SIP Trunk → OpenAI**, webhook registered at `PUBLIC_BASE_URL/openai/webhook` with ngrok running, then `scripts/check_sip.py` and a direct test call (A2, A3, A15). Depends on P0-1. | yes, onboarding step |

### P1: important before relying on it

| # | Fix |
|---|---|
| P1-1 | Native-speaker review of `app/phrases_lt.py`, the greeting first (legal disclosure) (S2). |
| P1-2 | Replace the draft prompts with the final A1/A2 texts, or approve the drafts (S1). |
| P1-3 | Fill `PRIVATE_TERMS` and `OWNER_PHONE_NUMBER`, and create `rules.yaml` (S5, S6). Without them the private-info guard has almost nothing to match. |
| P1-4 | Build the adversarial runner (pytest `integration` test plus script) on `app/live/local.py` + `tests/adversarial_cases.py`, and run it once against the real models (S3). |
| P1-5 | Disclosure guarantee: test how reliably the greeting is spoken word for word (I8). If it is not reliable, evaluate `output_audio.fixture.play` with a pre-recorded disclosure (A14). |
| P1-6 | Resolve the contradictory `limit_reached` and wrap-up instructions on a third `ask_owner` (I5). |
| P1-7 | Handle `function_call_outputs_required` / `tool_outputs_required` errors: log, and send any missing outputs or fall back (S7). |
| P1-8 | Delete the legacy secrets from `.env` (Azure speech key, Google credentials path, Anthropic extras), and revoke the Azure/Google keys if they are no longer used. |

### P2: later

| # | Fix |
|---|---|
| P2-1 | `black` the one file (`tests/adversarial_cases.py:174`). |
| P2-2 | Cap `MAX_OWNER_QUESTIONS` at 2 in `Settings` (`le=2`) (I2). |
| P2-3 | Update CLAUDE.md's `OWNER_DECISION` / `OWNER_TIMEOUT` wording to the JSON statuses (I1). Fix the design doc status line, the §3 diagram, §6 re-attach and the TTS sentence (I3, I4, I6, I7). |
| P2-4 | Remove dead code (`sanitize_for_speech` and its test, the `format_report` `latency` parameter) and fix the stale comments in §3. |
| P2-5 | Sideband re-attach with backoff up to about 10 s instead of a single 0.5 s retry (I4), once real drop behavior has been seen. |
| P2-6 | Deploy target (Fly or Render account), build the Docker image once, and verify the Twilio failover idea (A17). |
| P2-7 | Hold music, only if the bridge path or fixtures become viable (S8). |
