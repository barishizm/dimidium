"""Process-wide call bookkeeping: background tasks, active calls, draining on shutdown."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class AppHost:
    """Built once in the app lifespan; LiveCall reaches it through the CallHost protocol."""

    # Set on SIGTERM: new calls are refused and /health reports "draining".
    draining: bool = False
    # Active calls: call SID -> coroutine that ends the call politely (server shutdown).
    _enders: dict[str, Callable[[], Awaitable[None]]] = field(default_factory=dict)
    # Calls and end-of-call bookkeeping still running (kept referenced, awaited on shutdown).
    _background: set[asyncio.Task[None]] = field(default_factory=set)

    def spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def drain(self, timeout: float) -> None:
        if self._background:
            await asyncio.wait(self._background, timeout=timeout)

    def track_call(self, call_sid: str, end_politely: Callable[[], Awaitable[None]]) -> None:
        self._enders[call_sid] = end_politely

    def untrack_call(self, call_sid: str) -> None:
        self._enders.pop(call_sid, None)

    async def end_active_calls(self, timeout: float) -> None:
        """Stop taking calls, say goodbye on every active call, wait up to `timeout` for them."""
        self.draining = True
        if not self._enders:
            return
        logger.info("Shutdown: ending %d active call(s) politely", len(self._enders))
        for call_sid, end_politely in list(self._enders.items()):
            try:
                await end_politely()
            except Exception:
                logger.exception("Could not end the call politely", extra={"call_sid": call_sid})
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._enders and loop.time() < deadline:
            await asyncio.sleep(0.2)
        if self._enders:
            logger.warning(
                "Shutdown: %d call(s) still active after %.0f s", len(self._enders), timeout
            )
