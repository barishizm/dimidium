import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Chat, Message, Update, User

from app.agent.tools import OwnerOption
from app.owner_channel import (
    CHOICE_REJECT,
    CHOICE_WRITE,
    EXPIRED_TEXT,
    REJECT_LABEL,
    REJECT_MEANING_EN,
    WRITE_LABEL,
    DecisionCallback,
    DecisionKind,
    OwnerChannel,
    OwnerDecision,
    build_router,
    decision_to_result,
    parse_callback,
)

OWNER = 1001
STRANGER = 2002
CALL_A = "CA" + "a" * 32
CALL_B = "CA" + "b" * 32
OPTIONS = [
    OwnerOption(id="door", label_tr="Kapıya bırak", meaning_en="Leave the parcel at the door."),
    OwnerOption(id="locker", label_tr="Paketomata", meaning_en="Deliver to a parcel locker."),
]


class FakeBot:
    def __init__(self, fail_send: bool = False) -> None:
        self.sent: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self._next_id = 100
        self._fail_send = fail_send

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> SimpleNamespace:
        if self._fail_send:
            raise ConnectionError("telegram down")
        self._next_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "message_id": self._next_id, **kwargs})
        return SimpleNamespace(message_id=self._next_id)

    async def edit_message_text(self, text: str, **kwargs: Any) -> None:
        self.edits.append({"text": text, **kwargs})

    async def edit_message_reply_markup(self, **kwargs: Any) -> None:
        self.edits.append({"text": None, **kwargs})


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def channel(bot: FakeBot, clock: Clock) -> OwnerChannel:
    return OwnerChannel(bot, OWNER, clock=clock)  # type: ignore[arg-type]


async def request(
    channel: OwnerChannel, call_sid: str = CALL_A
) -> tuple[asyncio.Future[OwnerDecision], int]:
    future = await channel.send_decision_request(
        call_sid, "DPD kurye, paket.", OPTIONS, caller_number="+37060000000"
    )
    message_id = next(p.message_id for c, p in channel._pending.items() if c == call_sid)
    assert message_id is not None
    return future, message_id


def buttons(sent: dict[str, Any]) -> list[tuple[str, str]]:
    rows = sent["reply_markup"].inline_keyboard
    return [(row[0].text, row[0].callback_data) for row in rows]


async def click(
    channel: OwnerChannel, data: str, message_id: int | None = None, user: int = OWNER
) -> str | None:
    return await channel.handle_callback(user, user, data, message_id)


async def test_request_message_has_header_and_buttons(channel: OwnerChannel, bot: FakeBot) -> None:
    await request(channel)
    sent = bot.sent[0]
    assert sent["chat_id"] == OWNER
    assert sent["text"].startswith(f"📞 Arama #{CALL_A[-4:]} · +37060000000")
    labels = [text for text, _ in buttons(sent)]
    assert labels == ["Kapıya bırak", "Paketomata", WRITE_LABEL, REJECT_LABEL]
    for _, data in buttons(sent):
        assert len(data.encode()) <= 64
        parsed = parse_callback(data)
        assert parsed is not None and parsed.call_sid == CALL_A


async def test_option_click_resolves_future_and_edits_message(
    channel: OwnerChannel, bot: FakeBot
) -> None:
    future, message_id = await request(channel)
    data = buttons(bot.sent[0])[1][1]
    assert await click(channel, data, message_id) == "Karar iletildi."
    assert future.result() == OwnerDecision(DecisionKind.OPTION, option_id="locker")
    assert len(bot.edits) == 1
    assert bot.edits[0]["message_id"] == message_id
    assert bot.edits[0]["text"].endswith("✅ Karar: Paketomata")
    assert bot.edits[0]["reply_markup"] is None
    assert channel.pending_call_sids == []


async def test_double_click_resolves_once(channel: OwnerChannel, bot: FakeBot) -> None:
    future, message_id = await request(channel)
    door, locker = buttons(bot.sent[0])[0][1], buttons(bot.sent[0])[1][1]
    await click(channel, door, message_id)
    answer = await click(channel, locker, message_id)
    assert answer is not None and "zaten" in answer
    assert future.result().option_id == "door"
    assert len(bot.edits) == 1


async def test_unauthorized_chat_is_ignored(channel: OwnerChannel, bot: FakeBot) -> None:
    future, message_id = await request(channel)
    data = buttons(bot.sent[0])[0][1]
    assert await click(channel, data, message_id, user=STRANGER) is None
    assert await channel.handle_callback(OWNER, STRANGER, data, message_id) is None
    assert await channel.handle_text(STRANGER, STRANGER, "Kapıya bırak") is None
    assert not future.done()
    assert bot.edits == []
    assert len(bot.sent) == 1


@pytest.mark.parametrize(
    "data",
    [
        "garbage",
        "od:only:two",
        f"xx:{CALL_A}:abcdef01:0",
        f"od:{CALL_A}:ABCDEF:0",
        f"od:{CALL_A}:abcdef01:7",
        f"od:{CALL_A}:abcdef01:0:extra",
        "od:bad sid!:abcdef01:0",
    ],
)
def test_parse_callback_rejects_tampered_data(data: str) -> None:
    assert parse_callback(data) is None


async def test_wrong_nonce_or_out_of_range_choice_does_not_resolve(
    channel: OwnerChannel, bot: FakeBot
) -> None:
    future, message_id = await request(channel)
    nonce = channel._pending[CALL_A].nonce
    other_nonce = "00000000" if nonce != "00000000" else "11111111"
    stale = DecisionCallback(call_sid=CALL_A, nonce=other_nonce, choice="0").pack()
    assert await click(channel, stale, message_id) == EXPIRED_TEXT
    out_of_range = DecisionCallback(call_sid=CALL_A, nonce=nonce, choice="3").pack()
    assert await click(channel, out_of_range, message_id) == "Geçersiz düğme."
    assert await click(channel, "garbage", message_id) == "Geçersiz düğme."
    assert not future.done()


async def test_reject_button(channel: OwnerChannel, bot: FakeBot) -> None:
    future, message_id = await request(channel)
    reject = DecisionCallback(
        call_sid=CALL_A, nonce=channel._pending[CALL_A].nonce, choice=CHOICE_REJECT
    ).pack()
    await click(channel, reject, message_id)
    assert future.result().kind is DecisionKind.REJECTED
    assert bot.edits[0]["text"].endswith("❌ Reddedildi")


async def test_timeout_resolves_with_timeout_and_edits_message(bot: FakeBot) -> None:
    channel = OwnerChannel(bot, OWNER)  # type: ignore[arg-type]
    decision = await channel.ask(CALL_A, "Kurye.", OPTIONS, timeout=0.05)
    assert decision.kind is DecisionKind.TIMEOUT
    assert bot.edits[0]["text"].endswith(EXPIRED_TEXT)
    assert bot.edits[0]["reply_markup"] is None
    # A click after the timeout is answered, and resolves nothing.
    late = buttons(bot.sent[0])[0][1]
    assert await click(channel, late, bot.sent[0]["message_id"]) is not None
    assert len(bot.edits) == 1


async def test_ask_returns_owner_decision_before_timeout(bot: FakeBot) -> None:
    channel = OwnerChannel(bot, OWNER)  # type: ignore[arg-type]
    task = asyncio.create_task(channel.ask(CALL_A, "Kurye.", OPTIONS, timeout=5))
    while not bot.sent:
        await asyncio.sleep(0)
    await click(channel, buttons(bot.sent[0])[0][1], bot.sent[0]["message_id"])
    assert (await task).option_id == "door"


async def test_ask_cancelled_when_call_ends(bot: FakeBot) -> None:
    channel = OwnerChannel(bot, OWNER)  # type: ignore[arg-type]
    task = asyncio.create_task(channel.ask(CALL_A, "Kurye.", OPTIONS, timeout=5))
    while not bot.sent:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert channel.pending_call_sids == []
    assert "Arayan kapattı" in bot.edits[0]["text"]


async def test_unknown_request_click_removes_buttons(channel: OwnerChannel, bot: FakeBot) -> None:
    data = DecisionCallback(call_sid=CALL_A, nonce="abcdef01", choice="0").pack()
    assert await click(channel, data, 55) == EXPIRED_TEXT
    assert bot.edits == [{"text": None, "chat_id": OWNER, "message_id": 55, "reply_markup": None}]


async def test_write_button_then_text_resolves_that_call(
    channel: OwnerChannel, bot: FakeBot
) -> None:
    future_a, _ = await request(channel, CALL_A)
    future_b, msg_b = await request(channel, CALL_B)
    write_b = DecisionCallback(
        call_sid=CALL_B, nonce=channel._pending[CALL_B].nonce, choice=CHOICE_WRITE
    ).pack()
    await click(channel, write_b, msg_b)
    assert "cevabınızı yazın" in bot.sent[-1]["text"]
    reply = await channel.handle_text(OWNER, OWNER, "  Komşuya bıraksın  ")
    assert reply == f"✅ İletildi (#{CALL_B[-4:]})."
    assert future_b.result() == OwnerDecision(DecisionKind.FREE_TEXT, text="Komşuya bıraksın")
    assert not future_a.done()
    assert bot.edits[-1]["text"].endswith("✍️ Cevabınız: Komşuya bıraksın")


async def test_single_pending_call_accepts_plain_text(channel: OwnerChannel) -> None:
    future, _ = await request(channel)
    await channel.handle_text(OWNER, OWNER, "Yarın gelsin")
    assert future.result().text == "Yarın gelsin"


async def test_multiple_pending_calls_require_a_choice(channel: OwnerChannel) -> None:
    future_a, msg_a = await request(channel, CALL_A)
    future_b, _ = await request(channel, CALL_B)
    reply = await channel.handle_text(OWNER, OWNER, "Yarın gelsin")
    assert reply is not None and WRITE_LABEL in reply
    assert not future_a.done() and not future_b.done()
    # Replying to a specific request message targets that call.
    await channel.handle_text(OWNER, OWNER, "Yarın gelsin", reply_to_message_id=msg_a)
    assert future_a.result().text == "Yarın gelsin"
    assert not future_b.done()


async def test_free_text_window_expires(channel: OwnerChannel, clock: Clock) -> None:
    future_a, msg_a = await request(channel, CALL_A)
    future_b, _ = await request(channel, CALL_B)
    write_a = DecisionCallback(
        call_sid=CALL_A, nonce=channel._pending[CALL_A].nonce, choice=CHOICE_WRITE
    ).pack()
    await click(channel, write_a, msg_a)
    clock.now += 121
    await channel.handle_text(OWNER, OWNER, "Geç kaldım")
    assert not future_a.done() and not future_b.done()


async def test_text_without_pending_call(channel: OwnerChannel) -> None:
    assert await channel.handle_text(OWNER, OWNER, "merhaba") == "Şu anda bekleyen arama yok."


async def test_second_request_for_same_call_rejected(channel: OwnerChannel) -> None:
    await request(channel)
    with pytest.raises(ValueError):
        await request(channel)


async def test_send_failure_leaves_nothing_pending() -> None:
    channel = OwnerChannel(FakeBot(fail_send=True), OWNER)  # type: ignore[arg-type]
    with pytest.raises(ConnectionError):
        await channel.send_decision_request(CALL_A, "Kurye.", OPTIONS)
    assert channel.pending_call_sids == []


async def test_send_info(bot: FakeBot) -> None:
    channel = OwnerChannel(bot, OWNER)  # type: ignore[arg-type]
    assert await channel.send_info("📴 Arama bitti") is True
    assert bot.sent[0]["text"] == "📴 Arama bitti"
    failing = OwnerChannel(FakeBot(fail_send=True), OWNER)  # type: ignore[arg-type]
    assert await failing.send_info("x") is False


@pytest.mark.parametrize(
    "decision, expected",
    [
        (OwnerDecision(DecisionKind.OPTION, option_id="door"), "Leave the parcel at the door."),
        (OwnerDecision(DecisionKind.REJECTED), REJECT_MEANING_EN),
    ],
)
def test_decision_to_result(decision: OwnerDecision, expected: str) -> None:
    result = decision_to_result(decision, OPTIONS)
    assert result.decision_text_en == expected
    assert not result.timed_out


def test_decision_to_result_free_text_and_timeout() -> None:
    free = decision_to_result(OwnerDecision(DecisionKind.FREE_TEXT, text="Komşuya"), OPTIONS)
    assert free.free_text_original == "Komşuya"
    assert decision_to_result(OwnerDecision(DecisionKind.TIMEOUT), OPTIONS).timed_out


async def test_router_drops_updates_from_strangers() -> None:
    class SpyChannel(OwnerChannel):
        def __init__(self) -> None:
            super().__init__(FakeBot(), OWNER)  # type: ignore[arg-type]
            self.seen: list[str] = []

        async def handle_text(self, *args: Any, **kwargs: Any) -> str | None:
            self.seen.append("text")
            return None

    spy = SpyChannel()
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(spy))
    bot = Bot("123456:" + "A" * 35)
    try:
        for user in (STRANGER, OWNER):
            message = Message(
                message_id=1,
                date=datetime.now(UTC),
                chat=Chat(id=user, type="private"),
                from_user=User(id=user, is_bot=False, first_name="X"),
                text="Kapıya bırak",
            )
            await dispatcher.feed_update(bot, Update(update_id=user, message=message))
    finally:
        await bot.session.close()
    assert spy.seen == ["text"]


# --- commands, silent reports, ring alerts with forwarding --------------------------------


async def test_commands_are_routed_to_the_handler(channel: OwnerChannel) -> None:
    seen: list[str] = []

    async def on_command(text: str) -> str:
        seen.append(text)
        return "ok"

    assert await channel.handle_text(OWNER, OWNER, "/calls")  # default status reply
    channel.on_command = on_command
    assert await channel.handle_text(OWNER, OWNER, "/mode off") == "ok"
    assert await channel.handle_text(STRANGER, STRANGER, "/mode off") is None
    assert seen == ["/mode off"]


async def test_send_info_can_be_silent(channel: OwnerChannel, bot: FakeBot) -> None:
    await channel.send_info("routine", silent=True)
    await channel.send_info("important")
    assert [m["disable_notification"] for m in bot.sent] == [True, False]


async def test_ring_alert_forward_button(channel: OwnerChannel, bot: FakeBot) -> None:
    from app.owner_channel import FORWARD_LABEL, ForwardCallback

    forwarded: list[str] = []

    async def on_forward(call_sid: str) -> str:
        forwarded.append(call_sid)
        return "📲 Arama telefonuna yönlendiriliyor."

    await channel.send_ring_alert(CALL_A, "📞 Agate arıyor", offer_forward=True)
    [alert] = bot.sent
    [[button]] = alert["reply_markup"].inline_keyboard
    assert button.text == FORWARD_LABEL
    assert ForwardCallback.unpack(button.callback_data).call_sid == CALL_A

    assert await channel.handle_forward(STRANGER, STRANGER, button.callback_data, 1) is None
    assert "ayarlı değil" in (
        await channel.handle_forward(OWNER, OWNER, button.callback_data, 1) or ""
    )
    channel.on_forward = on_forward
    toast = await channel.handle_forward(OWNER, OWNER, button.callback_data, alert["message_id"])
    assert toast and "yönlendiriliyor" in toast
    assert forwarded == [CALL_A]
    assert bot.edits[-1]["reply_markup"] is None
    assert await channel.handle_forward(OWNER, OWNER, "fw:bad id!", 1) == "Geçersiz düğme."


async def test_ring_alert_without_forward_has_no_buttons(
    channel: OwnerChannel, bot: FakeBot
) -> None:
    await channel.send_ring_alert(CALL_A, "📞 Mom arıyor", offer_forward=False)
    assert bot.sent[0]["reply_markup"] is None
