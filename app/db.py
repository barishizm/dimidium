"""SQLite storage for call logs (aiosqlite)."""

import json
from collections.abc import Sequence
from dataclasses import dataclass
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
    owner_decision  TEXT,
    end_reason      TEXT,
    summary         TEXT,
    follow_up       INTEGER NOT NULL DEFAULT 0,
    flagged         INTEGER NOT NULL DEFAULT 0,
    -- GDPR audit: was the AI + recording disclosure found in the agent's speech (NULL: unknown)
    disclosure_verified INTEGER,
    disclosure_resent   INTEGER NOT NULL DEFAULT 0
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

CREATE TABLE IF NOT EXISTS app_state (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""

# Columns added after the first release: (table, column, definition) for in-place upgrades.
_ADDED_COLUMNS = (
    ("calls", "end_reason", "TEXT"),
    ("calls", "summary", "TEXT"),
    ("calls", "follow_up", "INTEGER NOT NULL DEFAULT 0"),
    ("calls", "flagged", "INTEGER NOT NULL DEFAULT 0"),
    ("calls", "disclosure_verified", "INTEGER"),
    ("calls", "disclosure_resent", "INTEGER NOT NULL DEFAULT 0"),
)


@dataclass(frozen=True)
class CallRow:
    call_sid: str
    caller_number: str
    started_at: datetime
    summary: str | None
    end_reason: str | None
    follow_up: bool
    flagged: bool


@dataclass(frozen=True)
class MessageRow:
    id: int
    call_sid: str | None
    caller_number: str | None
    created_at: datetime
    message: str


async def init_db(db_path: str) -> None:
    """Create the database file (and parent directory) and all tables if missing."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.executescript(SCHEMA)
        for table, column, definition in _ADDED_COLUMNS:
            async with db.execute(f"PRAGMA table_info({table})") as cursor:
                existing = {row[1] for row in await cursor.fetchall()}
            if column not in existing:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
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


async def set_call_outcome(
    db_path: str,
    call_sid: str,
    *,
    summary: str | None,
    end_reason: str | None,
    follow_up: bool,
    flagged: bool,
    disclosure_verified: bool | None = None,
    disclosure_resent: bool = False,
) -> None:
    db = await _connect(db_path)
    try:
        await db.execute(
            "UPDATE calls SET summary = ?, end_reason = ?, follow_up = ?, flagged = ?, "
            "disclosure_verified = ?, disclosure_resent = ? WHERE call_sid = ?",
            (
                summary,
                end_reason,
                int(follow_up),
                int(flagged),
                None if disclosure_verified is None else int(disclosure_verified),
                int(disclosure_resent),
                call_sid,
            ),
        )
        await db.commit()
    finally:
        await db.close()


async def recent_calls(db_path: str, limit: int = 10) -> list[CallRow]:
    db = await _connect(db_path)
    try:
        async with db.execute(
            "SELECT call_sid, caller_number, started_at, summary, end_reason, follow_up, flagged "
            "FROM calls ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
    finally:
        await db.close()
    return [
        CallRow(
            call_sid=r[0],
            caller_number=r[1],
            started_at=datetime.fromisoformat(r[2]),
            summary=r[3],
            end_reason=r[4],
            follow_up=bool(r[5]),
            flagged=bool(r[6]),
        )
        for r in rows
    ]


async def undelivered_messages(db_path: str, limit: int = 20) -> list[MessageRow]:
    """Oldest first: messages the owner has not seen in Telegram yet."""
    db = await _connect(db_path)
    try:
        async with db.execute(
            "SELECT m.id, m.call_sid, c.caller_number, m.created_at, m.message "
            "FROM messages_for_owner m LEFT JOIN calls c ON c.call_sid = m.call_sid "
            "WHERE m.delivered = 0 ORDER BY m.created_at, m.id LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
    finally:
        await db.close()
    return [
        MessageRow(
            id=r[0],
            call_sid=r[1],
            caller_number=r[2],
            created_at=datetime.fromisoformat(r[3]),
            message=r[4],
        )
        for r in rows
    ]


async def count_undelivered_messages(db_path: str) -> int:
    db = await _connect(db_path)
    try:
        async with db.execute("SELECT COUNT(*) FROM messages_for_owner WHERE delivered = 0") as c:
            row = await c.fetchone()
    finally:
        await db.close()
    return int(row[0]) if row else 0


async def mark_messages_delivered(db_path: str, ids: Sequence[int]) -> None:
    if not ids:
        return
    db = await _connect(db_path)
    try:
        await db.executemany(
            "UPDATE messages_for_owner SET delivered = 1 WHERE id = ?", [(i,) for i in ids]
        )
        await db.commit()
    finally:
        await db.close()


async def get_state(db_path: str, key: str) -> str | None:
    db = await _connect(db_path)
    try:
        async with db.execute("SELECT value FROM app_state WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
    finally:
        await db.close()
    return str(row[0]) if row else None


async def set_state(db_path: str, key: str, value: str) -> None:
    db = await _connect(db_path)
    try:
        await db.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await db.commit()
    finally:
        await db.close()


async def delete_transcripts_before(db_path: str, cutoff: datetime) -> int:
    """Delete transcript turns of calls that started before `cutoff`. Returns rows deleted.

    Call rows, summaries and messages for the owner are kept.
    """
    db = await _connect(db_path)
    try:
        cursor = await db.execute(
            "DELETE FROM transcript_turns WHERE call_sid IN "
            "(SELECT call_sid FROM calls WHERE started_at < ?)",
            (cutoff.isoformat(),),
        )
        deleted = cursor.rowcount
        await db.commit()
    finally:
        await db.close()
    return deleted


async def delete_records_before(db_path: str, cutoff: datetime) -> tuple[int, int]:
    """Delete calls that started before `cutoff` (their transcripts go with them) and owner
    messages created before it. Returns (calls deleted, messages deleted)."""
    db = await _connect(db_path)
    try:
        messages = await db.execute(
            "DELETE FROM messages_for_owner WHERE created_at < ?", (cutoff.isoformat(),)
        )
        calls = await db.execute("DELETE FROM calls WHERE started_at < ?", (cutoff.isoformat(),))
        deleted = (calls.rowcount, messages.rowcount)
        await db.commit()
    finally:
        await db.close()
    return deleted
