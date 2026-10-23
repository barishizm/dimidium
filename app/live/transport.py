"""OpenAI call control (accept / reject / hangup) and the sideband WebSocket, behind small
protocols so the call logic can be tested with fakes.

Endpoints (guides/voice-sip, openai SDK 3.36 `client.live.sessions` / `client.live.sideband`):
- POST /v1/live/sessions/{id}/accept   {"session": {...}}   200, empty body
- POST /v1/live/sessions/{id}/reject   {"status_code": 486} first accept/reject wins
- POST /v1/live/sessions/{id}/hangup   no body
- wss://api.openai.com/v1/live/sessions/{id}/attach        sideband; never send session.start
"""

import asyncio
import json
import logging
from typing import Any, Protocol

from openai import AsyncOpenAI

from app.config import Settings

logger = logging.getLogger(__name__)


class SidebandDisconnected(Exception):
    """The sideband WebSocket dropped (not a clean close after the session ended)."""


class CallControl(Protocol):
    async def accept(self, session_id: str, session: dict[str, Any]) -> None: ...

    async def reject(self, session_id: str, status_code: int) -> None: ...

    async def hangup(self, session_id: str) -> None: ...


class SidebandLink(Protocol):
    async def send(self, event: dict[str, Any]) -> None:
        """Send one client event. Raises SidebandDisconnected when the socket is gone."""
        ...

    async def recv(self) -> dict[str, Any] | None:
        """The next server event; None after a clean close. Raises SidebandDisconnected."""
        ...

    async def close(self) -> None: ...


class SidebandConnector(Protocol):
    async def attach(self, session_id: str) -> SidebandLink: ...


def create_openai_client(settings: Settings) -> AsyncOpenAI:
    if settings.openai_api_key is None:
        raise ValueError("OPENAI_API_KEY is not set")
    secret = settings.openai_webhook_secret
    return AsyncOpenAI(
        api_key=settings.openai_api_key.get_secret_value(),
        webhook_secret=secret.get_secret_value() if secret else None,
        timeout=settings.live_control_timeout_seconds,
        # One retry for call control; the sideband reconnect policy is ours (max_retries=0).
        max_retries=1,
    )


class OpenAICallControl:
    def __init__(self, client: AsyncOpenAI, timeout: float) -> None:
        self._client = client
        self._timeout = timeout

    async def accept(self, session_id: str, session: dict[str, Any]) -> None:
        await self._client.live.sessions.accept(
            session_id,
            session=session,  # type: ignore[arg-type]  # built in app.live.config
            timeout=self._timeout,
        )

    async def reject(self, session_id: str, status_code: int) -> None:
        await self._client.live.sessions.reject(
            session_id, status_code=status_code, timeout=self._timeout
        )

    async def hangup(self, session_id: str) -> None:
        await self._client.live.sessions.hangup(session_id, timeout=self._timeout)


class _OpenAISidebandLink:
    def __init__(self, manager: Any, connection: Any) -> None:
        self._manager = manager
        self._connection = connection

    async def send(self, event: dict[str, Any]) -> None:
        from websockets.exceptions import ConnectionClosed

        try:
            await self._connection.send(event)
        except ConnectionClosed as exc:
            raise SidebandDisconnected(f"send failed: {exc}") from exc

    async def recv(self) -> dict[str, Any] | None:
        from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

        try:
            # Raw JSON: unknown event types pass through instead of failing to parse.
            data = await self._connection.recv_bytes()
        except ConnectionClosedOK:
            return None
        except ConnectionClosedError as exc:
            raise SidebandDisconnected(str(exc)) from exc
        event = json.loads(data)
        return event if isinstance(event, dict) else {}

    async def close(self) -> None:
        await self._manager.__aexit__(None, None, None)


class OpenAISidebandConnector:
    def __init__(self, client: AsyncOpenAI, timeout: float) -> None:
        self._client = client
        self._timeout = timeout

    async def attach(self, session_id: str) -> SidebandLink:
        manager = self._client.live.sideband.connect(
            session_id=session_id,
            # session.closed is followed by a clean close instead of a dropped socket.
            graceful_close=True,
            # No silent SDK reconnects: LiveCall re-attaches once itself and knows about it.
            max_retries=0,
        )
        try:
            connection = await asyncio.wait_for(manager.enter(), self._timeout)
        except Exception as exc:
            raise SidebandDisconnected(f"attach failed: {type(exc).__name__}: {exc}") from exc
        return _OpenAISidebandLink(manager, connection)
