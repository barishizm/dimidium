"""Daily transcript cleanup (GDPR storage limitation). Raw audio is never stored at all."""

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


async def retention_loop(
    db_path: str, retention_days: int, interval: float = CLEANUP_INTERVAL_SECONDS
) -> None:
    """Purge once at startup, then every `interval` seconds. Never raises (except cancel)."""
    while True:
        try:
            await purge_old_transcripts(db_path, retention_days)
        except Exception:
            logger.exception("Transcript cleanup failed; retrying at the next run")
        await asyncio.sleep(interval)
