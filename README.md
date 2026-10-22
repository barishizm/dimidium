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
- While the owner is asked on Telegram, the caller hears generated hold music, plus "still
  trying" every `HOLD_PHRASE_INTERVAL_SECONDS`.
- Never silent: a filler phrase after `FILLER_AFTER_SECONDS`, and a stuck model turn counts as
  a failure. STT, LLM and TTS are each retried once. After that the agent takes a message with
  cached phrases only (or, if STT is down, says it cannot hear the caller), notifies you and
  hangs up.
- Limits: `MAX_CALL_SECONDS`, `MAX_CALLER_TURNS` (the model is told to wrap up shortly before
  either), `CALLER_IDLE_SECONDS`.
- Per-turn latency (estimated end of speech → first agent audio) is logged as `turn latency`,
  split into STT final and first LLM sentence.
- Every call's transcript goes to SQLite; a summary goes to Telegram after the call.

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

## Docker

```bash
docker build -t dimidium .
docker run --rm -p 8000:8000 --env-file .env dimidium
```

## Layout

| Path | Purpose |
| --- | --- |
| `app/main.py` | FastAPI app and `/health` endpoint |
| `app/config.py` | Settings loaded from `.env` (see `.env.example`) |
| `app/models.py` | `CallSession` and related data classes |
| `app/session_registry.py` | In-memory registry of active calls, keyed by call SID |
| `app/db.py` | SQLite schema: `calls`, `transcript_turns`, `messages_for_owner` |
| `app/logging_setup.py` | JSON logging with partial phone-number redaction |
| `app/agent/prompts.py` | System prompt (cached) plus per-call context block |
| `app/agent/tools.py` | Tool definitions, Pydantic input schemas, owner-reply parsing |
| `app/agent/brain.py` | `AgentBrain`: streaming Messages API loop, tool execution, sentence chunking |
| `app/owner_channel.py` | Telegram owner channel: decision requests, buttons, Futures, alerts |
| `app/rules.py` | Rules engine for `rules.yaml` (known numbers, keyword rules) |
| `app/speech/base.py` | `STTProvider` / `TTSProvider` interfaces, config and event types |
| `app/speech/*_provider.py` | Google (Chirp 3 + Chirp3-HD), Azure, Deepgram (STT), ElevenLabs (TTS) |
| `app/speech/factory.py` | Builds providers from `STT_PROVIDER` / `TTS_PROVIDER` |
| `app/speech/audio.py` | G.711 mu-law codec, WAV helpers, audio-file loader for benchmarks |
| `app/speech/phrases_lt.py` | Fixed Lithuanian phrases (pending native-speaker review) |
| `app/speech/metrics.py` | WER/CER, latency percentiles, report tables |
| `scripts/benchmark_speech.py` | Lithuanian STT/TTS benchmark on simulated phone audio |
| `scripts/simulate.py` | Text-only call simulator |
| `scripts/check_anthropic.py` | Anthropic API key check |
| `scripts/check_telegram.py` | Telegram token check, chat ID finder, test message |
| `app/telephony/routes.py` | `POST /twilio/voice` (signature check, TwiML) and `WS /twilio/stream` |
| `app/telephony/twilio_webhook.py` | Twilio signature validation, TwiML builders |
| `app/telephony/pipeline.py` | Builds and runs the Pipecat pipeline for one call |
| `app/telephony/agent_service.py` | `AgentBrainService`: brain in the pipeline, hold, fillers, limits, fallbacks |
| `app/telephony/speech_services.py` | Pipecat STT/TTS services over our providers (retry, cache) |
| `app/telephony/call_handlers.py` | Tool handlers for a call (Telegram, hold, message store), DB recorder |
| `app/telephony/phrase_cache.py` | Pre-synthesized fixed phrases, cached on disk per voice |
| `app/telephony/hold_audio.py` | Generated hold music |
| `app/telephony/latency.py` | Per-turn latency tracking and logging |
| `scripts/fake_twilio_call.py` | Local test call: plays Twilio's part, records the agent |
| `scripts/check_twilio.py` | Twilio credentials, number, webhook and tunnel check |
| `docs/live_call_test.md` | ngrok + Twilio setup and the live test-call checklist |
