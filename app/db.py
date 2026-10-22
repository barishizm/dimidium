"""SQLite storage for call logs (aiosqlite)."""

from pathlib import Path

import aiosqlite

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
