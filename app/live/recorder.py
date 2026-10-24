"""Writes a call's row and new transcript turns to SQLite as the call goes on."""

import logging

from app import db
from app.models import CallSession
from app.owner_channel import short_call_id

logger = logging.getLogger(__name__)


class CallRecorder:
    """Never raises: a database problem must not break the call."""

    def __init__(self, db_path: str, session: CallSession) -> None:
        self._db_path = db_path
        self._session = session
        self._written = 0

    async def flush(self) -> None:
        try:
            await db.upsert_call(self._db_path, self._session)
            turns = self._session.transcript[self._written :]
            await db.add_transcript_turns(self._db_path, self._session.call_sid, turns)
            self._written += len(turns)
        except Exception:
            logger.exception("Could not store call %s", short_call_id(self._session.call_sid))
