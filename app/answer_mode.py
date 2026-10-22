"""Who the agent answers: the owner flips this with /mode in Telegram. Persisted in SQLite."""

import logging
from enum import StrEnum

from app import db

logger = logging.getLogger(__name__)

_STATE_KEY = "answer_mode"


class AnswerMode(StrEnum):
    SCREEN_ALL = "screen_all"  # the agent screens every caller and asks the owner
    KNOWN_ONLY = "known_only"  # known numbers are screened; everyone else leaves a message
    OFF = "off"  # do not answer: voicemail (or reject); nothing is pushed to Telegram

    @property
    def label_tr(self) -> str:
        return _LABELS[self]

    def next(self) -> "AnswerMode":
        modes = list(AnswerMode)
        return modes[(modes.index(self) + 1) % len(modes)]


_LABELS = {
    AnswerMode.SCREEN_ALL: "🟢 Herkesi karşıla (screen all)",
    AnswerMode.KNOWN_ONLY: "🟡 Sadece kayıtlı numaralar (known numbers only)",
    AnswerMode.OFF: "🔴 Kapalı - cevaplama (off)",
}

# Words accepted after /mode, besides the enum values.
ALIASES = {
    "all": AnswerMode.SCREEN_ALL,
    "screen": AnswerMode.SCREEN_ALL,
    "on": AnswerMode.SCREEN_ALL,
    "known": AnswerMode.KNOWN_ONLY,
    "off": AnswerMode.OFF,
}


class AnswerModeStore:
    """The current mode, cached in memory so the voice webhook never waits on the database."""

    def __init__(self, db_path: str, mode: AnswerMode = AnswerMode.SCREEN_ALL) -> None:
        self._db_path = db_path
        self._mode = mode

    @classmethod
    async def load(cls, db_path: str) -> "AnswerModeStore":
        stored = await db.get_state(db_path, _STATE_KEY)
        try:
            mode = AnswerMode(stored) if stored else AnswerMode.SCREEN_ALL
        except ValueError:
            logger.warning("Unknown stored answer mode %r; using screen_all", stored)
            mode = AnswerMode.SCREEN_ALL
        return cls(db_path, mode)

    @property
    def mode(self) -> AnswerMode:
        return self._mode

    async def set(self, mode: AnswerMode) -> AnswerMode:
        await db.set_state(self._db_path, _STATE_KEY, mode.value)
        self._mode = mode
        logger.info("Answer mode set to %s", mode)
        return mode

    async def cycle(self) -> AnswerMode:
        return await self.set(self._mode.next())
