"""Production entry point: uvicorn with a polite shutdown for active calls.

    python -m app.serve

On SIGTERM (deploy, restart, scale-down) plain uvicorn shuts down before the app's lifespan
cleanup can end calls, so the sideband of every call would drop mid-sentence. Here the server first
marks itself draining (new calls are refused, /health returns 503), lets every active call say
"sorry, we have to end the call, I will pass everything on, goodbye", waits up to
SHUTDOWN_GRACE_SECONDS for those calls to end, and only then lets uvicorn close connections.

For development keep using `uvicorn app.main:app --reload`.
"""

import socket

import uvicorn

from app.config import get_settings
from app.host import AppHost
from app.main import app

# Seconds uvicorn waits for remaining connections after the calls have ended.
_UVICORN_GRACE_SECONDS = 5


class GracefulServer(uvicorn.Server):
    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        host: AppHost | None = getattr(app.state, "host", None)
        if host is not None and not self.force_exit:
            await host.end_active_calls(get_settings().shutdown_grace_seconds)
        await super().shutdown(sockets)


def main() -> None:
    settings = get_settings()
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=settings.port,
        # Fly/Render terminate TLS and set X-Forwarded-For. Trusting every hop makes
        # request.client the left-most, sender-written entry, so the webhook rate limit reads
        # the headers itself (app.live.webhook.client_ip).
        proxy_headers=True,
        forwarded_allow_ips="*",
        timeout_graceful_shutdown=_UVICORN_GRACE_SECONDS,
    )
    GracefulServer(config).run()


if __name__ == "__main__":
    main()
