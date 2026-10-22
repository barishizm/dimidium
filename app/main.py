"""FastAPI entry point."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI

from app.config import Settings, get_settings
from app.db import init_db
from app.logging_setup import setup_logging, setup_pipecat_logging
from app.owner_channel import start_telegram
from app.rules import RulesEngine
from app.speech.base import MissingCredentialsError, VoiceConfig
from app.speech.factory import create_stt, create_tts
from app.speech.phrases_lt import PHRASES
from app.telephony.phrase_cache import PhraseCache
from app.telephony.pipeline import TelephonyDeps
from app.telephony.routes import router as twilio_router

logger = logging.getLogger(__name__)


def check_speech(settings: Settings) -> str | None:
    """None if STT and TTS can be built, else a description of what is missing."""
    problems: list[str] = []
    for kind, factory in (("STT", create_stt), ("TTS", create_tts)):
        try:
            factory(settings)
        except MissingCredentialsError as exc:
            problems.append(f"{kind} {exc}")
    return "; ".join(problems) or None


async def warm_phrases(settings: Settings, phrases: PhraseCache) -> None:
    """Pre-synthesize the fixed phrases so the greeting plays without TTS latency."""
    tts = create_tts(settings)
    try:
        failed = await phrases.warm(
            tts,
            VoiceConfig(language=settings.caller_language),
            [p.text for p in PHRASES],
            timeout=settings.speech_timeout_seconds,
        )
    finally:
        await tts.aclose()
    if failed:
        logger.warning(
            "Phrase cache incomplete: %d of %d phrases missing", len(failed), len(PHRASES)
        )
    else:
        logger.info("Phrase cache ready (%d phrases)", len(phrases))


def voice_id(settings: Settings) -> str:
    voices = {
        "google": settings.google_tts_voice,
        "azure": settings.azure_tts_voice,
        "elevenlabs": f"{settings.elevenlabs_voice_id}/{settings.elevenlabs_model}",
    }
    return f"{settings.tts_provider}:{voices.get(settings.tts_provider, '')}"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings.log_level)
    setup_pipecat_logging(settings.pipecat_log_level)
    await init_db(settings.db_path)
    telegram = await start_telegram(settings)
    # None when Telegram is not configured; call handling then falls back to taking messages.
    app.state.owner_channel = telegram.channel if telegram else None

    speech_problem = check_speech(settings)
    if speech_problem:
        logger.warning("Calls will be refused until speech is configured: %s", speech_problem)
    phrases = PhraseCache(Path(settings.phrase_cache_dir), voice_id(settings))
    app.state.telephony = TelephonyDeps(
        settings=settings,
        rules=RulesEngine.from_yaml(settings.rules_path),
        owner_channel=app.state.owner_channel,
        phrases=phrases,
        speech_problem=speech_problem,
    )
    warm_task = None if speech_problem else asyncio.create_task(warm_phrases(settings, phrases))
    logger.info("startup complete", extra={"db_path": settings.db_path})
    try:
        yield
    finally:
        # Let calls that just ended finish storing transcripts and notifying the owner.
        await app.state.telephony.drain(timeout=15)
        if warm_task is not None:
            warm_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await warm_task
        if telegram is not None:
            await telegram.stop()
        logger.info("shutdown")


app = FastAPI(title="dimidium", lifespan=lifespan)
app.include_router(twilio_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
