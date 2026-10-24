"""Several calls at once through one owner channel: no cross-talk, nothing left behind."""

import asyncio
from typing import Any

from app.functions import AskOwnerInput, AskOwnerResult
from app.models import CallSession
from app.owner_channel import DecisionCallback, OwnerChannel, make_ask_owner_handler
from app.session_registry import CallSessionRegistry
from tests.test_owner_channel import OWNER, FakeBot

CALLS = {
    "CA" + "1" * 32: ("door", "Leave it at the door."),
    "CA" + "2" * 32: ("locker", "Send it to a parcel locker."),
    "CA" + "3" * 32: ("tomorrow", "Come back tomorrow."),
}


def ask_args(call_sid: str) -> dict[str, Any]:
    # Every call offers all three options; only the owner's click decides which applies.
    return {
        "summary_tr": f"Kurye {call_sid[-4:]}",
        "caller_said_lt": "siuntinys",
        "options": [
            {"id": option_id, "label_tr": option_id, "meaning_en": meaning}
            for option_id, meaning in CALLS.values()
        ],
    }


async def test_three_concurrent_calls_get_their_own_owner_decisions() -> None:
    bot = FakeBot()
    channel = OwnerChannel(bot, OWNER)  # type: ignore[arg-type]
    registry = CallSessionRegistry()
    sessions = {sid: CallSession(call_sid=sid, caller_number="+37060000000") for sid in CALLS}
    asks: dict[str, asyncio.Task[AskOwnerResult]] = {}
    for sid, session in sessions.items():
        registry.add(session)
        handler = make_ask_owner_handler(channel, session, timeout=5)
        asks[sid] = asyncio.create_task(handler(AskOwnerInput.model_validate(ask_args(sid))))
    while len(channel.pending_call_sids) < len(CALLS):
        await asyncio.sleep(0)

    pending = dict(channel._pending)
    # Forged cross-call click: call 1's id with call 2's nonce must not resolve anything.
    sids = list(CALLS)
    forged = DecisionCallback(call_sid=sids[0], nonce=pending[sids[1]].nonce, choice="0").pack()
    await channel.handle_callback(OWNER, OWNER, forged, None)
    assert set(channel.pending_call_sids) == set(CALLS)

    # The owner answers in reverse order; each call picks "its" option index.
    for index, sid in reversed(list(enumerate(sids))):
        data = DecisionCallback(call_sid=sid, nonce=pending[sid].nonce, choice=str(index)).pack()
        assert await channel.handle_callback(OWNER, OWNER, data, None) == "Karar iletildi."

    for sid, task in asks.items():
        assert (await task).decision_text_en == CALLS[sid][1]
        registry.remove(sid)

    assert channel.pending_call_sids == []
    assert len(registry) == 0


async def test_concurrent_timeouts_and_hang_ups_leave_nothing_pending() -> None:
    bot = FakeBot()
    channel = OwnerChannel(bot, OWNER)  # type: ignore[arg-type]
    options = AskOwnerInput.model_validate(ask_args("CA" + "1" * 32)).options

    timed_out = asyncio.create_task(channel.ask("CA" + "1" * 32, "a", options, timeout=0.05))
    hung_up = asyncio.create_task(channel.ask("CA" + "2" * 32, "b", options, timeout=5))
    answered = asyncio.create_task(channel.ask("CA" + "3" * 32, "c", options, timeout=5))
    while len(channel.pending_call_sids) < 3:
        await asyncio.sleep(0)
    hung_up.cancel()
    nonce = channel._pending["CA" + "3" * 32].nonce
    await channel.handle_callback(
        OWNER,
        OWNER,
        DecisionCallback(call_sid="CA" + "3" * 32, nonce=nonce, choice="1").pack(),
        None,
    )
    assert (await timed_out).kind == "timeout"
    assert (await answered).option_id == "locker"
    await asyncio.gather(hung_up, return_exceptions=True)
    assert channel.pending_call_sids == []
