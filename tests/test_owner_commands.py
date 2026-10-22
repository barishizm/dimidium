from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app import db
from app.answer_mode import AnswerMode, AnswerModeStore
from app.models import CallSession, TranscriptTurn
from app.owner_commands import HELP_TEXT, OwnerCommands, mask_number
from app.retention import purge_old_transcripts
from app.rules import RulesConfig

RULES = RulesConfig.model_validate(
    {
        "known_numbers": [{"number": "+37060000002", "name": "Mom", "relation": "family"}],
        "keyword_rules": [{"name": "courier", "keywords": ["DPD"]}],
        "do_not_answer": {
            "allow": [
                {
                    "number": "+37060000001",
                    "name": "Agate",
                    "alert_tr": "Agate arıyor",
                    "offer_forward": True,
                }
            ],
            "deny": [{"number": "+37060000009", "note": "spam"}],
        },
    }
)


@pytest.fixture
async def db_path(tmp_path: Path) -> str:
    path = str(tmp_path / "calls.db")
    await db.init_db(path)
    return path


@pytest.fixture
async def commands(db_path: str) -> OwnerCommands:
    return OwnerCommands(
        db_path=db_path,
        modes=await AnswerModeStore.load(db_path),
        rules=RULES,
        off_mode_action="voicemail",
        status_lines=lambda: ["Aktif arama: 0"],
    )


async def add_call(db_path: str, sid: str, started: datetime, **outcome: object) -> None:
    session = CallSession(call_sid=sid, caller_number="+37060000000", started_at=started)
    await db.upsert_call(db_path, session)
    await db.add_transcript_turns(db_path, sid, [TranscriptTurn("caller", "Labas", at=started)])
    if outcome:
        await db.set_call_outcome(db_path, sid, **outcome)  # type: ignore[arg-type]


async def test_mode_cycles_and_persists(commands: OwnerCommands, db_path: str) -> None:
    assert "known numbers only" in await commands.handle("/mode")
    assert "off" in await commands.handle("/mode")
    assert "screen all" in await commands.handle("/mode")
    assert "off" in await commands.handle("/mode off")
    assert (await AnswerModeStore.load(db_path)).mode is AnswerMode.OFF
    assert "known" in await commands.handle("/mode@jarvis_bot known")
    assert "Bilinmeyen mod" in await commands.handle("/mode sometimes")


async def test_stored_mode_garbage_falls_back(db_path: str) -> None:
    await db.set_state(db_path, "answer_mode", "nonsense")
    assert (await AnswerModeStore.load(db_path)).mode is AnswerMode.SCREEN_ALL


async def test_calls_lists_newest_first(commands: OwnerCommands, db_path: str) -> None:
    assert await commands.handle("/calls") == "Henüz arama yok."
    now = datetime.now(UTC)
    await add_call(
        db_path,
        "CA1",
        now - timedelta(hours=2),
        summary="DPD kuryesi",
        end_reason="done",
        follow_up=False,
        flagged=False,
    )
    await add_call(
        db_path,
        "CA2",
        now - timedelta(hours=1),
        summary="Kod istedi",
        end_reason="done",
        follow_up=True,
        flagged=True,
    )
    await add_call(db_path, "CA3", now)
    reply = await commands.handle("/calls")
    assert reply.index("(özet yok)") < reply.index("🚩") < reply.index("DPD kuryesi")


async def test_messages_shows_undelivered_once(commands: OwnerCommands, db_path: str) -> None:
    assert "Okunmamış mesaj yok" in await commands.handle("/messages")
    await add_call(db_path, "CA1", datetime.now(UTC))
    await db.add_owner_message(db_path, "CA1", "Jonas: geri arayın", delivered=False)
    await db.add_owner_message(db_path, "CA1", "already pushed", delivered=True)
    reply = await commands.handle("/messages")
    assert "Jonas: geri arayın" in reply and "already pushed" not in reply
    assert "+37060000000" in reply
    assert "Okunmamış mesaj yok" in await commands.handle("/messages")


async def test_rules_masks_numbers(commands: OwnerCommands) -> None:
    reply = await commands.handle("/rules")
    assert "Agate …001" in reply and "yönlendirme teklif edilir" in reply
    assert "…009 spam" in reply and "Mom (family) …002" in reply
    assert "+3706" not in reply
    assert "sesli mesaj" in reply


async def test_status(commands: OwnerCommands, db_path: str) -> None:
    await db.add_owner_message(db_path, None, "x", delivered=False)
    reply = await commands.handle("/status")
    assert "Mod: 🟢" in reply and "Aktif arama: 0" in reply and "Okunmamış mesaj: 1" in reply


async def test_unknown_command_shows_help(commands: OwnerCommands) -> None:
    assert await commands.handle("/start") == HELP_TEXT


async def test_database_error_is_reported(commands: OwnerCommands, tmp_path: Path) -> None:
    commands._db_path = str(tmp_path / "missing" / "x.db")
    assert "Komut çalışmadı" in await commands.handle("/calls")


def test_mask_number() -> None:
    assert mask_number("+370 600 00123") == "…123"
    assert mask_number(None) == "gizli"


async def test_retention_deletes_only_old_transcripts(db_path: str) -> None:
    now = datetime.now(UTC)
    await add_call(db_path, "CA_OLD", now - timedelta(days=31))
    await add_call(db_path, "CA_NEW", now - timedelta(days=29))
    assert await purge_old_transcripts(db_path, 30) == 1
    assert [c.call_sid for c in await db.recent_calls(db_path)] == ["CA_NEW", "CA_OLD"]
    assert await purge_old_transcripts(db_path, 30) == 0


async def test_init_db_upgrades_an_old_calls_table(tmp_path: Path) -> None:
    import aiosqlite

    path = str(tmp_path / "old.db")
    async with aiosqlite.connect(path) as conn:
        await conn.execute(
            "CREATE TABLE calls (call_sid TEXT PRIMARY KEY, caller_number TEXT NOT NULL, "
            "started_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL, "
            "collected_info TEXT NOT NULL DEFAULT '{}', owner_decision TEXT)"
        )
        await conn.commit()
    await db.init_db(path)
    await db.init_db(path)  # idempotent
    await add_call(
        path,
        "CA1",
        datetime.now(UTC),
        summary="s",
        end_reason="r",
        follow_up=True,
        flagged=False,
    )
    [row] = await db.recent_calls(path)
    assert (row.summary, row.end_reason, row.follow_up, row.flagged) == ("s", "r", True, False)
