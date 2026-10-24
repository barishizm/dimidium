"""Daily cleanup (GDPR storage limitation). Raw audio is never stored at all.

Transcripts go after RETENTION_DAYS; call rows, summaries and caller messages (also personal
data) after RECORDS_RETENTION_DAYS."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from app import db

logger = logging.getLogger(__name__)

CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60


async def purge_old_transcripts(db_path: str, retention_days: int) -> int:
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    deleted = await db.delete_transcripts_before(db_path, cutoff)
    if deleted:
        logger.info("Deleted %d transcript lines older than %d days", deleted, retention_days)
    return deleted


async def purge_old_records(db_path: str, records_retention_days: int) -> tuple[int, int]:
    cutoff = datetime.now(UTC) - timedelta(days=records_retention_days)
    calls, messages = await db.delete_records_before(db_path, cutoff)
    if calls or messages:
        logger.info(
            "Deleted %d calls and %d messages older than %d days",
            calls,
            messages,
            records_retention_days,
        )
    return calls, messages


async def retention_loop(
    db_path: str,
    retention_days: int,
    records_retention_days: int,
    interval: float = CLEANUP_INTERVAL_SECONDS,
) -> None:
    """Purge once at startup, then every `interval` seconds. Never raises (except cancel)."""
    while True:
        try:
            await purge_old_transcripts(db_path, retention_days)
            await purge_old_records(db_path, records_retention_days)
        except Exception:
            logger.exception("Data cleanup failed; retrying at the next run")
        await asyncio.sleep(interval)
