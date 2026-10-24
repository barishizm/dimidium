# dimidium

Dimidium is your AI agent. It can answer your phone calls, email, messages and more.

The first component is "Jarvis", an AI phone receptionist. It answers forwarded calls in
Lithuanian, gathers the caller's information, asks the owner via Telegram (in Turkish), and
relays the decision back to the caller on the same call. See [CLAUDE.md](CLAUDE.md) for the full
specification and [docs/GPT_LIVE_DESIGN.md](docs/GPT_LIVE_DESIGN.md) for the design.

> Status: GPT-Live SIP path, Telegram owner channel, call logs and post-call reports built and
> tested with fakes. Not yet on a real phone call (the Twilio number is still pending; see
> [docs/SETUP_SIP.md](docs/SETUP_SIP.md)).

## How a call works

```
caller -> your mobile (conditional forwarding) -> Twilio number -> Elastic SIP Trunk
       -> OpenAI GPT-Live (speech in/out, turn-taking) -> Responses delegation (gpt-6-luna)
                 |                                              |
     webhook: live.transport.incoming            function calls: ask_owner, save_message,
                 v                                  flag_suspicious, end_call
       this app: screen, accept, then follow the call over a sideband WebSocket
                 -> runs the functions, asks you on Telegram, stores transcripts
```

- Audio never passes through this app. GPT-Live talks to the caller by itself on one call leg;
  the app watches the transcripts and events on the sideband and sends instructions back.
- The voice model (`LIVE_VOICE_MODEL`, default `gpt-live-1`) delegates reasoning to a backend
  Responses model (`LIVE_BACKEND_MODEL`, default `gpt-6-luna`). Only our four functions are
  exposed; hosted tools (web search, files, code) are off.
- Every function call is validated in code against a strict Pydantic model before it runs
  ([app/functions.py](app/functions.py)); unknown functions and bad arguments get an error
  output, never a side effect. `ask_owner` is capped at `MAX_OWNER_QUESTIONS` (default 2) per
  call.
- The opening line carries the AI + recording disclosure. The app checks the agent's transcript
  for it and makes the agent say it if it is missing.
- On hold, GPT-Live keeps the caller company: a hold announcement, then up to
  `HOLD_STILL_TRYING_MAX` "still trying" lines every `HOLD_PHRASE_INTERVAL_SECONDS`. There is
  no hold music on SIP ([docs/HOLD_MUSIC.md](docs/HOLD_MUSIC.md)). What the caller says on hold
  reaches the backend with your decision, marked as untrusted caller speech.
- Never silent: a failed backend turn is retried once; after that, or when the sideband cannot
  be re-attached, the agent apologizes, takes a message, you are told, and the call ends.
- Limits: `MAX_CALL_SECONDS` (the agent is told to wrap up `WRAP_UP_SECONDS` before), the
  goodbye is allowed to finish before the hangup.

Who gets the agent is decided when the webhook arrives ([app/live/sip.py](app/live/sip.py)):

1. Caller ID is our own `TWILIO_PHONE_NUMBER` (a forwarding loop): rejected.
2. `do_not_answer.deny` in `rules.yaml`: rejected (busy).
3. Server shutting down, `MAX_CONCURRENT_CALLS` reached, or one number calling more than
   `CALLS_PER_CALLER` times in `CALLS_PER_CALLER_WINDOW_SECONDS`: rejected (busy).
4. `do_not_answer.allow`: always screened, whatever the mode. You get "📞 Agate arıyor" in
   Telegram at once. Calls are never transferred.
5. Mode 🔴 off: voicemail through the agent (it greets, takes a message and never asks you;
   nothing is pushed to Telegram, read it with `/messages`), or busy with
   `OFF_MODE_ACTION=reject`.
6. Mode 🟡 known numbers only: numbers in `rules.yaml` are screened; everyone else leaves a
   voicemail (pushed to Telegram as usual).
7. Otherwise the normal screening agent.

## Requirements

- Python 3.11+
- For real calls: an OpenAI project with billing, a Twilio number with an Elastic SIP Trunk, a
  Telegram bot, and a public HTTPS URL ([docs/SETUP_SIP.md](docs/SETUP_SIP.md),
  [docs/SETUP.md](docs/SETUP.md)).

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # fill in values as features need them
cp rules.example.yaml rules.yaml   # call-screening rules (git-ignored)
```

## Try the agent without a phone

`scripts/live_smoke.py` opens one GPT-Live session over a WebSocket with the production session
config, streams a caller recording into it, prints both transcripts and every function call,
and answers `ask_owner` with a simulated owner decision. Needs `OPENAI_API_KEY`.

```bash
pip install -e ".[audio]"
python scripts/live_smoke.py --print-config          # the session config, no API call
python scripts/live_smoke.py --audio caller.wav --owner-reply "Leave it at the door."
```

## Telegram owner channel

The agent asks you through a Telegram bot: one message per question, with a button per option
plus "✍️ Kendim yazarım" (type your own answer) and "❌ Reddet / Kapat". Only the chat in
`TELEGRAM_OWNER_CHAT_ID` is listened to; everyone else is ignored, and nothing said on the phone
can grant owner authority. If you do not answer within `HOLD_TIMEOUT_SECONDS`, the message
changes to "⏱ Süre doldu" and the agent takes a message.

With one call waiting, any text you send is the answer. With several, press "✍️ Kendim yazarım"
on the right message first (your next text within 2 minutes goes to that call), or reply to that
message. Free text may be Turkish or English; the agent relays its meaning in Lithuanian.

```bash
python scripts/check_telegram.py   # checks the token, prints your chat ID / sends a test
```

Commands (also in the bot's menu):

| Command | What it does |
| --- | --- |
| `/calls` | Last 10 calls with their summaries (🚩 flagged, 📌 needs follow-up) |
| `/messages` | Messages callers left that you have not seen in Telegram yet (shown once, then marked read) |
| `/mode` | Cycles the answer mode: 🟢 screen all → 🟡 known numbers only → 🔴 off. `/mode all`, `/mode known`, `/mode off` set it directly. Survives restarts. |
| `/rules` | The loaded `rules.yaml` (numbers masked to the last 3 digits) |
| `/status` | Mode, active calls, pending questions, GPT-Live and summary readiness |

After every call you get a short Turkish report: who, why, what was decided, what the caller was
told, and what to follow up. It comes from a separate Responses call to the backend model
(`SUMMARY_PROVIDER`/`SUMMARY_MODEL`; Claude is an option) once the call has ended, so it never
slows the call down; if that call fails, you get the plain facts instead.
Routine calls arrive silently; calls with a message left, a suspicious-call flag, a hang-up on
hold or a follow-up arrive with a normal notification.

The FastAPI app starts the bot with long polling on startup. Only one process may poll a bot at a
time: stop `uvicorn` before running the check script.

## Run

```bash
uvicorn app.main:app --reload
curl http://localhost:8000/health   # {"status":"ok"}
```

On startup the app creates the SQLite database at `DB_PATH` (default `data/dimidium.db`). Every
call's transcript goes there; a daily cleanup deletes transcripts older than `RETENTION_DAYS`
(30) and call rows, summaries and messages older than `RECORDS_RETENTION_DAYS` (365). The app
never receives or stores audio. What OpenAI, Twilio and Telegram get and keep:
[docs/PRIVACY.md](docs/PRIVACY.md).

## Test and lint

```bash
pytest -q
ruff check . && black --check .
```

## Deployment

Full setup (Twilio number, SIP trunk, OpenAI webhook, call forwarding codes, Telegram bot,
costs): [docs/SETUP_SIP.md](docs/SETUP_SIP.md) and [docs/SETUP.md](docs/SETUP.md). Check any
environment with `python scripts/doctor.py` (env vars, OpenAI models, Telegram, Twilio, public
URL) and `python scripts/check_sip.py` (trunk, origination URI, signed test webhook).

### The container

- `Dockerfile`: multi-stage `python:3.12-slim-bookworm`. The app runs as uid 10001. The
  entrypoint starts as root only to hand a freshly mounted volume to that user (Fly and Render
  mount volumes root-owned), then drops root with `setpriv`.
- `CMD python -m app.serve` ([app/serve.py](app/serve.py)): uvicorn with proxy headers and a
  **polite shutdown**. On SIGTERM the app stops taking calls (new calls are rejected,
  `/health` returns 503 `draining`), every active call hears "sorry, we have to end the call,
  I will pass on everything you said, goodbye", you get the usual Telegram report, and only
  then do connections close. `SHUTDOWN_GRACE_SECONDS` (20 s) bounds the wait, so the
  platform's kill timeout must be longer (30 s in the configs below).
- `GET /health`: liveness (200 `ok`, 503 `draining`). Missing optional keys do not fail it, so
  a misconfigured app reports its problems in `/status` and the doctor instead of restart loops.
- Data lives in `/srv/data` (SQLite database). Mount a volume there, or every redeploy loses
  the call log.

Locally:

```bash
docker compose up --build                     # app on http://localhost:8000, fully non-root
docker compose exec app python scripts/doctor.py
```

### Single small container on Fly.io or Render

Run **exactly one** always-on instance in the EU. Why:

- **One instance:** the Telegram bot uses long polling (only one process may poll), call
  sessions live in memory, and SQLite is a local file. Never scale it out.
- **Always on, no cold starts:** the incoming-call webhook must be accepted within seconds, and
  the bot must keep polling. Scale-to-zero and free sleeping plans are out.
- **EU region close to Lithuania:** Fly.io `arn` (Stockholm) or `fra`; Render `frankfurt`.
- **Deploys restart the app:** with a volume, the old instance stops before the new one starts,
  so there is a gap of roughly 20-60 s where calls are not answered. Deploy when `/status`
  shows no active calls. Calls in progress get the polite goodbye above.
- **Memory:** the app only handles events, not audio; 512 MB is enough.

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
     OPENAI_API_KEY=... OPENAI_WEBHOOK_SECRET=... OPENAI_PROJECT_ID=... \
     TELEGRAM_BOT_TOKEN=... TELEGRAM_OWNER_CHAT_ID=... \
     TWILIO_ACCOUNT_SID=... TWILIO_AUTH_TOKEN=... TWILIO_PHONE_NUMBER=+370...
   # rules.yaml: a base64-encoded secret, written to /srv/config at boot.
   fly secrets set RULES_YAML_B64="$(base64 -i rules.yaml | tr -d '\n')"
   ```

   Then uncomment the `[[files]]` block in `fly.toml` (a missing secret fails the deploy).
   Any other `.env` value (timeouts, limits) goes in the same way or under `[env]`.
5. `fly deploy`. Then `fly ssh console -C "python scripts/doctor.py"`.
6. Point the OpenAI project webhook at `https://<app>.fly.dev/openai/webhook`
   ([docs/SETUP_SIP.md](docs/SETUP_SIP.md), step 3).

`fly.toml` already sets `min_machines_running = 1`, `auto_stop_machines = "off"`, a `/health`
check, `kill_timeout = 30`, and a `shared-cpu-1x` machine with 512 MB.
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
4. Service → **Environment → Secret Files**: add `rules.yaml`. It appears at `/etc/secrets/`.
5. Deploy, then open **Shell** and run `python scripts/doctor.py`.
6. Point the OpenAI project webhook at `https://<service>.onrender.com/openai/webhook`.

`autoDeploy` is off so a `git push` never restarts the app in the middle of a call; deploy
from the dashboard (**Manual Deploy**) when no call is active.

## Layout

| Path | Purpose |
| --- | --- |
| `app/main.py` | FastAPI app, lifespan wiring and `/health` |
| `app/serve.py` | Production entry point: uvicorn with a polite shutdown for active calls |
| `app/host.py` | Background tasks, active calls, draining on shutdown |
| `app/live/webhook.py` | `POST /openai/webhook`: signature check, de-duplication, accept / reject |
| `app/live/sip.py` | Caller number from the SIP headers, screening decision |
| `app/live/call.py` | One call: sideband events, functions, hold, disclosure check, limits, fallbacks |
| `app/live/config.py` | GPT-Live session config: voice prompt, Responses delegation, functions |
| `app/live/prompts.py` | Voice and backend prompts |
| `app/live/transport.py` | OpenAI call control (accept / reject / hangup) and the sideband |
| `app/live/transcript.py` | Transcript deltas into turns; disclosure check |
| `app/live/recorder.py` | Writes call rows and transcript turns to SQLite during the call |
| `app/functions.py` | Function schemas, Pydantic argument models, owner-reply parsing |
| `app/guards.py` | Caller-risk detection, private-detail guard, speech sanitizer |
| `app/phrases_lt.py` | Fixed Lithuanian phrases (pending native-speaker review) |
| `app/owner_channel.py` | Telegram owner channel: decision requests, buttons, Futures, alerts |
| `app/owner_commands.py` | `/calls`, `/messages`, `/mode`, `/rules`, `/status` |
| `app/answer_mode.py` | Answer mode (screen all / known only / off), persisted in SQLite |
| `app/post_call.py` | Post-call Turkish summary (separate model call) and the Telegram report |
| `app/retention.py` | Daily deletion of transcripts older than `RETENTION_DAYS` |
| `app/rules.py` | Rules engine for `rules.yaml` (known numbers, keyword rules, allow/deny lists) |
| `app/rate_limit.py` | Sliding-window rate limiter (calls per caller) |
| `app/config.py` | Settings loaded from `.env` (see `.env.example`) |
| `app/models.py` | `CallSession` and related data classes |
| `app/session_registry.py` | In-memory registry of active calls, keyed by call SID |
| `app/db.py` | SQLite schema: `calls` (with summary), `transcript_turns`, `messages_for_owner`, `app_state` |
| `app/logging_setup.py` | JSON logging with phone-number redaction and secret masking |
| `scripts/live_smoke.py` | One GPT-Live WebSocket session with a recorded caller |
| `scripts/check_sip.py` | SIP setup check: project, webhook secret, signed test webhook, Twilio trunk |
| `scripts/check_telegram.py` | Telegram token check, chat ID finder, test message |
| `scripts/doctor.py` | Whole-setup check (env, OpenAI, Telegram, Twilio, public URL) as a pass/fail table |
| `docs/SETUP_SIP.md` | OpenAI project, webhook, Twilio SIP trunk, first test call |
| `docs/SETUP.md` | Twilio number, GSM call forwarding, Telegram bot, cost per minute |
| `docs/GPT_LIVE_DESIGN.md` | Design: event flow, functions, hold, errors |
| `docs/PRIVACY.md` | GDPR: who processes what, retention, OpenAI `store` settings, open points |
| `Dockerfile`, `docker-entrypoint.sh`, `docker-compose.yml` | Container image (non-root) and local compose run |
| `fly.toml`, `render.yaml` | Single always-on EU instance on Fly.io or Render |
