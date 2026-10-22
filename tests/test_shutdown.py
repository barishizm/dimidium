"""Graceful shutdown: every active call is asked to end politely, then awaited."""

import asyncio

import pytest

from app.telephony.pipeline import TelephonyDeps


class FakeCall:
    """Ends (untracks itself) shortly after being asked to say goodbye."""

    def __init__(self, deps: TelephonyDeps, call_sid: str, fail: bool = False) -> None:
        self.deps, self.call_sid, self.fail = deps, call_sid, fail
        self.asked = False

    async def end_politely(self) -> None:
        self.asked = True
        if self.fail:
            raise RuntimeError("pipeline already gone")
        asyncio.get_running_loop().call_later(0.1, self.deps.untrack_call, self.call_sid)


def make_deps() -> TelephonyDeps:
    return TelephonyDeps(
        settings=None,  # type: ignore[arg-type]
        rules=None,  # type: ignore[arg-type]
        owner_channel=None,
        phrases=None,  # type: ignore[arg-type]
        modes=None,  # type: ignore[arg-type]
        forwarder=None,  # type: ignore[arg-type]
    )


async def test_end_active_calls_asks_every_call_and_waits() -> None:
    deps = make_deps()
    calls = [FakeCall(deps, "CA1"), FakeCall(deps, "CA2")]
    for call in calls:
        deps.track_call(call.call_sid, call.end_politely)
    await deps.end_active_calls(timeout=5)
    assert deps.draining
    assert all(call.asked for call in calls)
    assert not deps._enders


async def test_one_failing_call_does_not_block_the_others() -> None:
    deps = make_deps()
    broken, fine = FakeCall(deps, "CA1", fail=True), FakeCall(deps, "CA2")
    deps.track_call("CA1", broken.end_politely)
    deps.track_call("CA2", fine.end_politely)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await deps.end_active_calls(timeout=0.5)
    assert fine.asked
    assert list(deps._enders) == ["CA1"]  # gave up on it after the timeout
    assert loop.time() - started < 2


async def test_no_calls_returns_at_once() -> None:
    deps = make_deps()
    await deps.end_active_calls(timeout=30)
    assert deps.draining


async def test_server_ends_calls_before_uvicorn_closes_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uvicorn

    from app import serve

    order: list[str] = []
    deps = make_deps()
    deps.track_call("CA1", lambda: _record(order, "goodbye"))

    async def uvicorn_shutdown(self: object, sockets: object = None) -> None:
        order.append("uvicorn closes connections")

    monkeypatch.setattr(uvicorn.Server, "shutdown", uvicorn_shutdown)
    monkeypatch.setattr(serve.app.state, "telephony", deps, raising=False)
    monkeypatch.setenv("SHUTDOWN_GRACE_SECONDS", "0")
    serve.get_settings.cache_clear()
    server = serve.GracefulServer(uvicorn.Config(serve.app))
    await server.shutdown()
    assert order == ["goodbye", "uvicorn closes connections"]
    assert deps.draining


async def _record(order: list[str], event: str) -> None:
    order.append(event)
