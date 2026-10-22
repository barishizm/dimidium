"""Several calls at once through one owner channel: no cross-talk, nothing left behind."""

import asyncio
from typing import Any

from app.agent.brain import AgentBrain, BrainConfig
from app.agent.prompts import build_system_blocks
from app.agent.tools import AskOwnerInput, ToolHandlers
from app.models import CallSession
from app.owner_channel import DecisionCallback, OwnerChannel, make_ask_owner_handler
from app.session_registry import CallSessionRegistry
from tests.test_brain import FakeClient, collect, message, text, tool_use
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


async def noop(args: Any) -> None:
    return None


def make_call(call_sid: str, channel: OwnerChannel) -> tuple[AgentBrain, FakeClient]:
    session = CallSession(call_sid=call_sid, caller_number="+37060000000")
    client = FakeClient(
        message(
            text("Prašau palaukti, nepadėkite ragelio."), tool_use("ask_owner", ask_args(call_sid))
        ),
        message(text("Gerai.")),
    )
    handlers = ToolHandlers(
        ask_owner=make_ask_owner_handler(channel, session, timeout=5),
        save_message=noop,
        end_call=noop,
        flag_suspicious=noop,
    )
    brain = AgentBrain(
        client,  # type: ignore[arg-type]
        BrainConfig(model="test-model"),
        handlers,
        build_system_blocks("X", 75, "-"),
        session,
    )
    return brain, client


def tool_result_text(client: FakeClient) -> str:
    last_user = client.requests[-1]["messages"][-1]
    return str(last_user["content"][0]["content"])


async def test_three_concurrent_calls_get_their_own_owner_decisions() -> None:
    bot = FakeBot()
    channel = OwnerChannel(bot, OWNER)  # type: ignore[arg-type]
    registry = CallSessionRegistry()
    calls = {sid: make_call(sid, channel) for sid in CALLS}
    for brain, _ in calls.values():
        registry.add(brain._session)  # type: ignore[arg-type]

    turns = [
        asyncio.create_task(collect(brain.respond("Turiu siuntinį"))) for brain, _ in calls.values()
    ]
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
    await asyncio.gather(*turns)

    for sid, (brain, client) in calls.items():
        result = tool_result_text(client)
        own_meaning = CALLS[sid][1]
        assert f"OWNER_DECISION: {own_meaning}" in result
        others = [m for s, (_, m) in CALLS.items() if s != sid]
        assert not any(m in result for m in others)
        assert brain._session.owner_decision == own_meaning  # type: ignore[union-attr]
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
