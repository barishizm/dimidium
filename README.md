# dimidium

Dimidium is your AI agent. It can answer your phone calls, email, messages and more.

The first component is "Jarvis", an AI phone receptionist. It answers forwarded calls in
Lithuanian, gathers the caller's information, asks the owner via Telegram (in Turkish), and
relays the decision back to the caller on the same call. See [CLAUDE.md](CLAUDE.md) for the full
specification.

> Status: agent brain, text simulator, Telegram owner channel, speech provider layer (with a
> benchmark) and the Twilio telephony pipeline built. Tested locally against fake Twilio
> calls; not yet on a real phone call (see [docs/live_call_test.md](docs/live_call_test.md)).

## Requirements

- Python 3.11+

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # fill in values as features need them
cp rules.example.yaml rules.yaml   # call-screening rules (git-ignored)
```

## Text simulator

Talk to the agent in the terminal, without telephony. You type as the caller; when the agent
calls `ask_owner`, you also answer as the owner (option number, option id, or free text).
Needs `ANTHROPIC_API_KEY` in `.env`.

```bash
python scripts/check_anthropic.py   # verifies the API key with one tiny request
python scripts/simulate.py --caller-number +37060000000
```

## Telegram owner channel

The agent asks you through a Telegram bot: one message per question, with a button per option
plus "✍️ Kendim yazarım" (type your own answer) and "❌ Reddet / Kapat". Only the chat in
`TELEGRAM_OWNER_CHAT_ID` is listened to; everyone else is ignored. If you do not answer within
`HOLD_TIMEOUT_SECONDS`, the message changes to "⏱ Süre doldu" and the agent takes a message.

With one call waiting, any text you send is the answer. With several, press "✍️ Kendim yazarım"
on the right message first (your next text within 2 minutes goes to that call), or reply to that
message.

```bash
python scripts/check_telegram.py              # checks the token, prints your chat ID / sends a test
python scripts/simulate.py --telegram         # simulator with the owner on Telegram
```

Commands (also in the bot's menu):

| Command | What it does |
| --- | --- |
| `/calls` | Last 10 calls with their summaries (🚩 flagged, 📌 needs follow-up) |
| `/messages` | Messages callers left that you have not seen in Telegram yet (shown once, then marked read) |
| `/mode` | Cycles the answer mode: 🟢 screen all → 🟡 known numbers only → 🔴 off. `/mode all`, `/mode known`, `/mode off` set it directly. Survives restarts. |
| `/rules` | The loaded `rules.yaml` (numbers masked to the last 3 digits) |
| `/status` | Mode, active calls, pending questions, speech/summary/forwarding readiness |

After every call you get a short Turkish report: who, why, what was decided, what the caller was
told, and what to follow up. It comes from a separate cheap model call (`SUMMARY_MODEL`) once
the call has ended, so it never slows the call down; without an Anthropic key, or if that call
fails, you get the plain facts instead. Routine calls arrive silently; calls with a message left,
a suspicious-call flag, a hang-up on hold or a follow-up arrive with a normal notification.

The FastAPI app starts the bot with long polling on startup. Only one process may poll a bot at a
time: stop `uvicorn` before running the simulator with `--telegram` (or the check script).

## Speech providers and benchmark

Speech-to-text and text-to-speech sit behind `STTProvider` / `TTSProvider`
([app/speech/base.py](app/speech/base.py)); all audio is 8 kHz mu-law, the Twilio format. Pick
providers with `STT_PROVIDER` (google | azure | deepgram) and `TTS_PROVIDER`
(google | azure | elevenlabs).

The benchmark measures Lithuanian quality on simulated phone audio before the voice pipeline is
built:

```bash
pip install -e ".[google,azure,bench]"
python scripts/benchmark_speech.py --skip-stt                 # TTS only: also checks your keys
python scripts/benchmark_speech.py --audio-dir samples/lt     # STT + TTS
```

- **STT:** put recordings in a folder (`.wav`/`.mp3`/`.flac`/`.ogg`), each with a reference
  transcript of the same name (`kurjeris_01.mp3` + `kurjeris_01.txt`). Files are converted to
  8 kHz mu-law and streamed at real-time pace with 1.5 s of trailing silence. The report shows
  WER and CER (case and punctuation ignored), first-partial latency (audio start to first text)
  and final latency (end of speech to last final result). `--no-realtime` gives a quick WER
  check, but its latencies mean nothing. `--languages lt-LT,en-US` tests auto-detection.
- **TTS:** the 10 phrases in [app/speech/phrases_lt.py](app/speech/phrases_lt.py) are saved to
  `out/tts/<provider>/NN_<key>.wav`, with time-to-first-byte. `out/tts/phrases.txt` is a review
  sheet for a native speaker. Try other voices with `--voice google=lt-LT-Chirp3-HD-Charon`.
- Providers with missing keys or SDK packages are skipped and listed at the end. Raw numbers go
  to `out/speech_benchmark.json`.

`samples/` and `out/` are git-ignored: real call recordings contain personal data.

## Telephony (Twilio + Pipecat)

```
caller -> Twilio number -> POST /twilio/voice (signed) -> TwiML <Connect><Stream>
       -> WS /twilio/stream -> Pipecat: transport.input -> Silero VAD -> STT -> user aggregator
          -> AgentBrainService (Claude, tool use) -> TTS -> transport.output -> assistant aggregator
```

- One call leg from start to finish. The stream only accepts the one-time token from our own
  TwiML, and the webhook rejects requests without a valid `X-Twilio-Signature`.
- STT and TTS are the same providers the benchmark measures, wrapped as Pipecat services
  ([app/telephony/speech_services.py](app/telephony/speech_services.py)).
- The greeting (AI + recording disclosure), fillers, hold phrase and fallback phrases are
  pre-synthesized at startup and cached in `PHRASE_CACHE_DIR`. They play without TTS latency,
  and they still play when live TTS is down.
- Barge-in: when the caller speaks, TTS stops, queued audio is dropped (including Twilio's
  buffer), and the model turn is cancelled. The caller is muted during the disclosure, on hold
  and while the agent says goodbye.
- Before asking you, the agent tells the caller to hold. The prompt requires it, and the code
  checks it: if the model forgot, the cached "please hold" phrase is spoken first. The Telegram
  message goes out right away, without waiting for that phrase to finish.
- While the owner is asked on Telegram, the caller hears hold music (`HOLD_AUDIO_PATH`, or
  generated music if that is empty or unusable), plus "still trying, thank you for waiting"
  every `HOLD_PHRASE_INTERVAL_SECONDS`. What the caller says on hold is transcribed and kept,
  never answered by the model: a canned "yes, I am here" plays (at most once per 6 s), and the
  buffered words reach the model together with your decision (`CALLER_SAID_DURING_HOLD`).
- If the caller hangs up on hold, the Telegram question changes to "📵 Arayan kapattı" and you
  get the collected information as a normal message.
- The agent may ask you at most `MAX_OWNER_QUESTIONS` times per call (default 2); later
  questions are refused in code and never reach Telegram.
- Custom hold music: an 8 kHz mono mu-law file, `.wav` or raw `.ulaw`, at least 1 s. It is
  turned down to the generated music's level if louder. `python scripts/make_hold_audio.py`
  writes the generated loop as `data/hold_music.wav`; convert your own music with
  `ffmpeg -i music.mp3 -ar 8000 -ac 1 -c:a pcm_mulaw data/my_hold.wav`.
- Never silent: a filler phrase after `FILLER_AFTER_SECONDS`, and a stuck model turn counts as
  a failure. STT, LLM and TTS are each retried once. After that the agent takes a message with
  cached phrases only (or, if STT is down, says it cannot hear the caller), notifies you and
  hangs up.
- Limits: `MAX_CALL_SECONDS`, `MAX_CALLER_TURNS` (the model is told to wrap up shortly before
  either), `CALLER_IDLE_SECONDS`.
- Per-turn latency (estimated end of speech → first agent audio) is logged as `turn latency`,
  split into STT final and first LLM sentence.
- Every call's transcript goes to SQLite; a summary goes to Telegram after the call (see the
  Telegram section). Transcripts older than `RETENTION_DAYS` (default 30) are deleted by a daily
  cleanup; call rows, summaries and messages stay. Raw audio is never stored.

Who gets the agent is decided in `POST /twilio/voice`, in this order:

1. Caller ID is our own `TWILIO_PHONE_NUMBER` (a forward bounced back from your phone): hang up.
2. `do_not_answer.deny` in `rules.yaml`: rejected with a busy signal.
3. `do_not_answer.allow`: always screened, whatever the mode. You get "📞 Agate arıyor" in
   Telegram at once; with `offer_forward: true` and `OWNER_FORWARD_NUMBER` set, a
   "📲 Telefonuma bağla" button forwards the live call to your phone via `<Dial>` (the one
   exception to the single-call-leg rule). If you do not pick up within `FORWARD_RING_SECONDS`,
   Twilio calls `POST /twilio/dial-done` and the agent takes a message.
4. Mode 🔴 off: voicemail through the agent (it greets, takes a message and never asks you;
   nothing is pushed to Telegram, read it with `/messages`), or a busy signal with
   `OFF_MODE_ACTION=reject`.
5. Mode 🟡 known numbers only: numbers in `rules.yaml` are screened; everyone else leaves a
   voicemail (pushed to Telegram as usual).
6. Otherwise the normal screening agent.

Test without a Twilio account (needs Anthropic + speech keys):

```bash
TWILIO_AUTH_TOKEN=local-test-token uvicorn app.main:app
TWILIO_AUTH_TOKEN=local-test-token python scripts/fake_twilio_call.py \
  --say "Laba diena, čia DPD kurjeris. Turiu jums siuntinį."
```

For a real phone call (ngrok, Twilio number, webhook, checklist), see
[docs/live_call_test.md](docs/live_call_test.md). `python scripts/check_twilio.py` verifies the
Twilio setup.

## Run

```bash
uvicorn app.main:app --reload
curl http://localhost:8000/health   # {"status":"ok"}
```

On startup the app creates the SQLite database at `DB_PATH` (default `data/dimidium.db`).

## Test and lint

```bash
pytest -q
ruff check . && black --check .
```

## Deployment

Full setup (Twilio number, webhook, call forwarding codes, Telegram bot, costs):
[docs/SETUP.md](docs/SETUP.md). Check any environment with `python scripts/doctor.py`: it
tests the env vars, the Twilio webhook (via the API), Telegram, the Anthropic key and the
speech credentials, and prints a pass/fail table.

### The container

- `Dockerfile`: multi-stage `python:3.12-slim-bookworm`. The app runs as uid 10001. The
  entrypoint starts as root only to hand a freshly mounted volume to that user (Fly and Render
  mount volumes root-owned), then drops root with `setpriv`. Build arg `EXTRAS` picks the
  speech SDKs (`google` by default, `google,azure` for Azure; Deepgram/ElevenLabs need none).
- `CMD python -m app.serve` ([app/serve.py](app/serve.py)): uvicorn with proxy headers and a
  **polite shutdown**. On SIGTERM the app stops taking calls (new calls hear "unavailable",
  `/health` returns 503 `draining`), every active call hears "sorry, we have to end the
  call, I will pass on everything you said, goodbye", you get the usual Telegram report with
  what the caller said, and only then do connections close. `SHUTDOWN_GRACE_SECONDS` (20 s)
  bounds the wait, so the platform's kill timeout must be longer (30 s in the configs below).
- `GET /health`: liveness (200 `ok`, 503 `draining`). Missing optional keys do not fail it, so
  a misconfigured app reports its problems in `/status` and the doctor instead of restart loops.
- Data lives in `/srv/data` (SQLite database, phrase cache). Mount a volume there, or every
  redeploy loses the call log.

Locally:

```bash
mkdir -p secrets && cp ~/path/to/google-key.json secrets/google.json   # if you use Google
docker compose up --build                     # app on http://localhost:8000, fully non-root
docker compose exec app python scripts/doctor.py
```

### Single small container on Fly.io or Render

Run **exactly one** always-on instance in the EU. Why:

- **One instance:** the Telegram bot uses long polling (only one process may poll), call
  sessions live in memory, and SQLite is a local file. Never scale it out.
- **Always on, no cold starts:** a caller must hear the greeting within a second or two, and
  the bot must keep polling. Scale-to-zero and free sleeping plans are out.
- **EU region close to Lithuania:** Fly.io `arn` (Stockholm) or `fra`; Render `frankfurt`.
  Twilio may handle a call's media in its US region by default, so measure the `turn latency`
  log lines after deploying rather than assuming the nearest region is fastest.
- **WebSockets:** both platforms pass them through on the normal HTTPS port; nothing to
  configure. `PUBLIC_BASE_URL` is the platform URL (or your custom domain).
- **Deploys restart the app:** with a volume, the old instance stops before the new one starts,
  so there is a gap of roughly 20-60 s where calls go unanswered. Deploy when `/status` shows
  no active calls. Calls in progress get the polite goodbye above.
- **Memory:** pipecat + Silero VAD + the speech SDKs need a few hundred MB. 1 GB is
  comfortable; 512 MB may be tight with several calls at once (watch the metrics).

Before you start: both need an account with a card. Neither is set up yet; follow the steps
below once and tell Claude when you are done so the next step can be verified.

#### Fly.io ([fly.toml](fly.toml))

1. Install the CLI: `brew install flyctl`, then `fly auth signup` (or `fly auth login`).
2. Pick a unique app name and set it in `fly.toml` (`app = "..."`), then create the app
   without deploying: `fly launch --no-deploy --copy-config`. Answer *no* to databases/Redis.
3. Volume for the call log: `fly volumes create dimidium_data --region arn --size 1`.
4. Secrets (encrypted, given to the app as env vars):

   ```bash
   fly secrets set PUBLIC_BASE_URL=https://<app>.fly.dev OWNER_NAME=... \
     ANTHROPIC_API_KEY=... TELEGRAM_BOT_TOKEN=... TELEGRAM_OWNER_CHAT_ID=... \
     TWILIO_ACCOUNT_SID=... TWILIO_AUTH_TOKEN=... TWILIO_PHONE_NUMBER=+370... \
     GOOGLE_CLOUD_PROJECT=...
   # Files: base64-encoded secrets, written to /srv/config at boot.
   fly secrets set GOOGLE_CREDENTIALS_B64="$(base64 -i secrets/google.json | tr -d '\n')"
   fly secrets set RULES_YAML_B64="$(base64 -i rules.yaml | tr -d '\n')"
   ```

   Then uncomment the two `[[files]]` blocks in `fly.toml` (a missing secret fails the deploy).
   Any other `.env` value (speech provider, timeouts) goes in the same way or under `[env]`.
5. `fly deploy`. Then `fly ssh console -C "python scripts/doctor.py"`.
6. Point Twilio at `https://<app>.fly.dev/twilio/voice` (docs/SETUP.md, step 2).

`fly.toml` already sets `min_machines_running = 1`, `auto_stop_machines = "off"`, a `/health`
check, `kill_timeout = 30`, and a `shared-cpu-1x` machine with 1 GB.
Logs: `fly logs`. After editing `rules.yaml`, set `RULES_YAML_B64` again (that restarts the app).

#### Render ([render.yaml](render.yaml))

1. Push the repo to GitHub/GitLab, sign up at <https://render.com> and connect the account.
2. Dashboard → **New → Blueprint** → pick the repo. Render reads `render.yaml`: a Docker web
   service on the **Starter** plan (paid; the free plan sleeps and cannot have a disk) in
   **Frankfurt**, 1 instance, a 1 GB disk at `/srv/data`, health check `/health`,
   `maxShutdownDelaySeconds: 30`.
3. Fill in the values it asks for. `PUBLIC_BASE_URL` is `https://<service>.onrender.com` (shown
   on the service page after the first deploy; set it then and redeploy if you do not know it
   yet).
4. Service → **Environment → Secret Files**: add `google.json` (the service-account key) and
   `rules.yaml`. They appear at `/etc/secrets/`.
5. Deploy, then open **Shell** and run `python scripts/doctor.py`.
6. Point Twilio at `https://<service>.onrender.com/twilio/voice`.

`autoDeploy` is off so a `git push` never restarts the app in the middle of a call; deploy
from the dashboard (**Manual Deploy**) when no call is active.

## Layout

| Path | Purpose |
| --- | --- |
| `app/main.py` | FastAPI app and `/health` endpoint |
| `app/serve.py` | Production entry point: uvicorn with a polite shutdown for active calls |
| `app/rate_limit.py` | Sliding-window rate limiter for `/twilio/voice` |
| `app/config.py` | Settings loaded from `.env` (see `.env.example`) |
| `app/models.py` | `CallSession` and related data classes |
| `app/session_registry.py` | In-memory registry of active calls, keyed by call SID |
| `app/db.py` | SQLite schema: `calls` (with summary), `transcript_turns`, `messages_for_owner`, `app_state` |
| `app/logging_setup.py` | JSON logging with partial phone-number redaction |
| `app/agent/prompts.py` | System prompt (cached) plus per-call context block |
| `app/agent/tools.py` | Tool definitions, Pydantic input schemas, owner-reply parsing |
| `app/agent/brain.py` | `AgentBrain`: streaming Messages API loop, tool execution, sentence chunking |
| `app/owner_channel.py` | Telegram owner channel: decision requests, buttons, Futures, alerts |
| `app/owner_commands.py` | `/calls`, `/messages`, `/mode`, `/rules`, `/status` |
| `app/answer_mode.py` | Answer mode (screen all / known only / off), persisted in SQLite |
| `app/post_call.py` | Post-call Turkish summary (separate model call) and the Telegram report |
| `app/retention.py` | Daily deletion of transcripts older than `RETENTION_DAYS` |
| `app/rules.py` | Rules engine for `rules.yaml` (known numbers, keyword rules, allow/deny lists) |
| `app/speech/base.py` | `STTProvider` / `TTSProvider` interfaces, config and event types |
| `app/speech/*_provider.py` | Google (Chirp 3 + Chirp3-HD), Azure, Deepgram (STT), ElevenLabs (TTS) |
| `app/speech/factory.py` | Builds providers from `STT_PROVIDER` / `TTS_PROVIDER` |
| `app/speech/audio.py` | G.711 mu-law codec, WAV helpers, audio-file loader for benchmarks |
| `app/speech/phrases_lt.py` | Fixed Lithuanian phrases (pending native-speaker review) |
| `app/speech/metrics.py` | WER/CER, latency percentiles, report tables |
| `scripts/benchmark_speech.py` | Lithuanian STT/TTS benchmark on simulated phone audio |
| `scripts/simulate.py` | Text-only call simulator |
| `scripts/make_hold_audio.py` | Writes the generated hold music as an 8 kHz mu-law WAV |
| `scripts/check_anthropic.py` | Anthropic API key check |
| `scripts/check_telegram.py` | Telegram token check, chat ID finder, test message |
| `app/telephony/routes.py` | `POST /twilio/voice` (signature, mode, allow/deny), `/twilio/dial-done`, `WS /twilio/stream` |
| `app/telephony/forwarding.py` | Forwards an allowlisted caller's live call to your phone (Twilio REST) |
| `app/telephony/twilio_webhook.py` | Twilio signature validation, TwiML builders |
| `app/telephony/pipeline.py` | Builds and runs the Pipecat pipeline for one call |
| `app/telephony/agent_service.py` | `AgentBrainService`: brain in the pipeline, hold, fillers, limits, fallbacks |
| `app/telephony/speech_services.py` | Pipecat STT/TTS services over our providers (retry, cache) |
| `app/telephony/call_handlers.py` | Tool handlers for a call (Telegram, hold, message store), DB recorder |
| `app/telephony/phrase_cache.py` | Pre-synthesized fixed phrases, cached on disk per voice |
| `app/telephony/hold_audio.py` | Hold music: generated loop or `HOLD_AUDIO_PATH` file |
| `app/telephony/latency.py` | Per-turn latency tracking and logging |
| `scripts/fake_twilio_call.py` | Local test call: plays Twilio's part, records the agent |
| `scripts/check_twilio.py` | Twilio credentials, number, webhook and tunnel check |
| `scripts/doctor.py` | Whole-setup check (env, Twilio webhook, Telegram, Anthropic, speech) as a pass/fail table |
| `docs/live_call_test.md` | ngrok + Twilio setup and the live test-call checklist |
| `docs/SETUP.md` | Twilio number, webhook, GSM call forwarding, Telegram bot, cost per minute |
| `Dockerfile`, `docker-entrypoint.sh`, `docker-compose.yml` | Container image (non-root) and local compose run |
| `fly.toml`, `render.yaml` | Single always-on EU instance on Fly.io or Render |
