"""FastAPI entry point."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.db import init_db
from app.logging_setup import setup_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings.log_level)
    await init_db(settings.db_path)
    logger.info("startup complete", extra={"db_path": settings.db_path})
    yield
    logger.info("shutdown")


app = FastAPI(title="dimidium", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
