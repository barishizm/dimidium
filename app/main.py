"""FastAPI entry point."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.answer_mode import AnswerModeStore
from app.config import Settings, get_settings
from app.db import init_db
from app.host import AppHost
from app.live.webhook import create_live_deps
from app.live.webhook import router as live_router
from app.logging_setup import setup_logging
from app.owner_channel import OwnerChannel, start_telegram
from app.owner_commands import OwnerCommands
from app.post_call import CallSummarizer
from app.rate_limit import SlidingWindowLimiter
from app.retention import retention_loop
from app.rules import RulesEngine
from app.session_registry import CALL_SESSION_REGISTRY

logger = logging.getLogger(__name__)


def status_lines(
    settings: Settings,
    channel: OwnerChannel | None,
    summarizer: CallSummarizer | None,
) -> list[str]:
    """Live state for /status (Turkish)."""
    summary_model = settings.summary_model if summarizer else "kapalı (API anahtarı yok)"
    return [
        f"Aktif arama: {len(CALL_SESSION_REGISTRY)}",
        f"Cevap bekleyen soru: {len(channel.pending_call_sids) if channel else 0}",
        f"Özet modeli: {summary_model}",
        f"GPT-Live (SIP): {live_status(settings)}",
        f"Transkript saklama: {settings.retention_days} gün",
    ]


def live_status(settings: Settings) -> str:
    if settings.openai_api_key is None:
        return "kapalı (OPENAI_API_KEY yok)"
    if settings.openai_webhook_secret is None:
        return "⚠️ OPENAI_WEBHOOK_SECRET yok"
    return "hazır"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings.log_level, settings.secret_values())
    await init_db(settings.db_path)
    telegram = await start_telegram(settings)
    # None when Telegram is not configured; calls then fall back to taking messages.
    channel = telegram.channel if telegram else None
    rules = RulesEngine.from_yaml(settings.rules_path)
    modes = await AnswerModeStore.load(settings.db_path)
    summarizer = CallSummarizer.from_settings(settings)
    host = AppHost()
    app.state.host = host
    app.state.live = create_live_deps(
        settings,
        host,
        rules=rules,
        modes=modes,
        owner_channel=channel,
        summarizer=summarizer,
        caller_limiter=SlidingWindowLimiter(
            settings.calls_per_caller, settings.calls_per_caller_window_seconds
        ),
    )
    if telegram is not None:
        commands = OwnerCommands(
            db_path=settings.db_path,
            modes=modes,
            rules=rules.config,
            off_mode_action=settings.off_mode_action,
            status_lines=lambda: status_lines(settings, channel, summarizer),
        )
        telegram.channel.on_command = commands.handle
    cleanup_task = asyncio.create_task(
        retention_loop(settings.db_path, settings.retention_days), name="transcript-retention"
    )
    logger.info("startup complete", extra={"db_path": settings.db_path})
    try:
        yield
    finally:
        # Let calls that just ended finish storing transcripts and notifying the owner.
        await host.drain(timeout=15)
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
        if telegram is not None:
            await telegram.stop()
        logger.info("shutdown")


app = FastAPI(title="dimidium", lifespan=lifespan)
app.include_router(live_router)


@app.get("/health")
async def health(request: Request) -> JSONResponse:
    """Liveness for Docker / Fly / Render. 503 while shutting down, so no new calls are routed
    here; missing optional keys do not fail it (that would only cause restart loops)."""
    host: AppHost | None = getattr(request.app.state, "host", None)
    if host is not None and host.draining:
        return JSONResponse({"status": "draining"}, status_code=503)
    return JSONResponse({"status": "ok"})
