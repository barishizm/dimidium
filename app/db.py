"""SQLite storage for call logs (aiosqlite)."""

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from app.models import CallSession, TranscriptTurn

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    call_sid        TEXT PRIMARY KEY,
    caller_number   TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    status          TEXT NOT NULL,
    collected_info  TEXT NOT NULL DEFAULT '{}',
    owner_decision  TEXT
);

CREATE TABLE IF NOT EXISTS transcript_turns (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    call_sid  TEXT NOT NULL REFERENCES calls(call_sid) ON DELETE CASCADE,
    speaker   TEXT NOT NULL,
    text      TEXT NOT NULL,
    at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transcript_turns_call ON transcript_turns(call_sid);

CREATE TABLE IF NOT EXISTS messages_for_owner (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    call_sid    TEXT REFERENCES calls(call_sid) ON DELETE SET NULL,
    created_at  TEXT NOT NULL,
    message     TEXT NOT NULL,
    delivered   INTEGER NOT NULL DEFAULT 0
);
"""


async def init_db(db_path: str) -> None:
    """Create the database file (and parent directory) and all tables if missing."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.executescript(SCHEMA)
        await db.commit()


async def _connect(db_path: str) -> aiosqlite.Connection:
    db = await aiosqlite.connect(db_path)
    await db.execute("PRAGMA foreign_keys = ON")
    return db


async def upsert_call(db_path: str, session: CallSession) -> None:
    """Insert the call row, or update its status, decision and end time."""
    db = await _connect(db_path)
    try:
        await db.execute(
            """
            INSERT INTO calls (call_sid, caller_number, started_at, ended_at, status,
                               collected_info, owner_decision)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(call_sid) DO UPDATE SET
                ended_at = excluded.ended_at,
                status = excluded.status,
                collected_info = excluded.collected_info,
                owner_decision = excluded.owner_decision
            """,
            (
                session.call_sid,
                session.caller_number,
                session.started_at.isoformat(),
                session.ended_at.isoformat() if session.ended_at else None,
                str(session.status),
                json.dumps(session.collected_info, ensure_ascii=False),
                session.owner_decision,
            ),
        )
        await db.commit()
    finally:
        await db.close()


async def add_transcript_turns(
    db_path: str, call_sid: str, turns: Sequence[TranscriptTurn]
) -> None:
    if not turns:
        return
    db = await _connect(db_path)
    try:
        await db.executemany(
            "INSERT INTO transcript_turns (call_sid, speaker, text, at) VALUES (?, ?, ?, ?)",
            [(call_sid, t.speaker, t.text, t.at.isoformat()) for t in turns],
        )
        await db.commit()
    finally:
        await db.close()


async def add_owner_message(
    db_path: str, call_sid: str | None, message: str, delivered: bool
) -> None:
    db = await _connect(db_path)
    try:
        await db.execute(
            "INSERT INTO messages_for_owner (call_sid, created_at, message, delivered) "
            "VALUES (?, ?, ?, ?)",
            (call_sid, datetime.now(UTC).isoformat(), message, int(delivered)),
        )
        await db.commit()
    finally:
        await db.close()
