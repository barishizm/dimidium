# dimidium

Dimidium is your AI agent. It can answer your phone calls, email, messages and more.

The first component is "Jarvis", an AI phone receptionist. It answers forwarded calls in
Lithuanian, gathers the caller's information, asks the owner via Telegram (in Turkish), and
relays the decision back to the caller on the same call. See [CLAUDE.md](CLAUDE.md) for the full
specification.

> Status: scaffold only. `/health` works; telephony, speech, LLM and Telegram are not built yet.

## Requirements

- Python 3.11+

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # fill in values as features need them
```

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
