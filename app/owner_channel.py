"""Telegram owner channel: decision requests with inline buttons, resolved via asyncio Futures.

Only the configured owner chat is ever listened to. Everything else is dropped silently.
"""

import asyncio
import logging
import secrets
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from pydantic import Field, ValidationError

from app.agent.tools import (
    MAX_OWNER_OPTIONS,
    AskOwnerInput,
    AskOwnerResult,
    OwnerOption,
    resolve_owner_reply,
)
from app.config import Settings
from app.models import CallSession

logger = logging.getLogger(__name__)

FREE_TEXT_WINDOW_SECONDS = 120
TELEGRAM_TIMEOUT_SECONDS = 10
# Telegram rejects messages above 4096 characters; leave room for header and outcome.
MAX_SUMMARY_CHARS = 3000
# Remembered outcomes, so late or double clicks get a precise answer.
FINISHED_HISTORY = 200

CHOICE_WRITE = "w"
CHOICE_REJECT = "x"
WRITE_LABEL = "✍️ Kendim yazarım"
REJECT_LABEL = "❌ Reddet / Kapat"
EXPIRED_TEXT = "⏱ Süre doldu"
# The call ended while the owner was being asked (in practice: the caller hung up on hold).
HUNG_UP_TEXT = "📵 Arayan kapattı"
FORWARD_LABEL = "📲 Telefonuma bağla"
# Telegram's command menu (descriptions in Turkish).
BOT_COMMANDS = (
    ("calls", "Son 10 arama ve özetleri"),
    ("messages", "Okunmamış arayan mesajları"),
    ("mode", "Cevaplama modu: herkes / kayıtlı / kapalı"),
    ("rules", "Yüklü kurallar"),
    ("status", "Durum"),
)
REJECT_MEANING_EN = (
    "The owner declines. Politely tell the caller the owner cannot help with this right now, "
    "offer to take a short message, then say goodbye and end the call."
)


class DecisionKind(StrEnum):
    OPTION = "option"
    FREE_TEXT = "free_text"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class OwnerDecision:
    kind: DecisionKind
    option_id: str | None = None
    text: str | None = None


class DecisionCallback(CallbackData, prefix="od"):
    """Button payload: which call, which question on that call, which choice."""

    call_sid: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    nonce: str = Field(pattern=r"^[0-9a-f]{8}$")
    choice: str = Field(pattern=rf"^([0-{MAX_OWNER_OPTIONS - 1}]|{CHOICE_WRITE}|{CHOICE_REJECT})$")


class ForwardCallback(CallbackData, prefix="fw"):
    """Button payload: forward this live call to the owner's real phone."""

    call_sid: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")


CommandHandler = Callable[[str], Awaitable[str]]
# Forwards the call; returns a short Turkish outcome for the owner.
ForwardHandler = Callable[[str], Awaitable[str]]


class TelegramBot(Protocol):
    """The subset of aiogram's Bot used here (lets tests pass a fake)."""

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any: ...

    async def edit_message_text(self, text: str, **kwargs: Any) -> Any: ...

    async def edit_message_reply_markup(self, **kwargs: Any) -> Any: ...


@dataclass
class PendingDecision:
    call_sid: str
    nonce: str
    text: str
    options: list[OwnerOption]
    future: asyncio.Future[OwnerDecision]
    message_id: int | None = None

    @property
    def short_id(self) -> str:
        return short_call_id(self.call_sid)


@dataclass
class _FreeTextTarget:
    call_sid: str
    nonce: str
    deadline: float


def short_call_id(call_sid: str) -> str:
    return call_sid[-4:]


def format_request(
    call_sid: str, summary_tr: str, caller_number: str | None, caller_said: str | None
) -> str:
    header = f"📞 Arama #{short_call_id(call_sid)}"
    if caller_number:
        header += f" · {caller_number}"
    summary = summary_tr.strip()
    if len(summary) > MAX_SUMMARY_CHARS:
        summary = summary[:MAX_SUMMARY_CHARS] + "…"
    parts = [header, summary]
    if caller_said:
        parts.append(f'🗣 "{caller_said.strip()[:500]}"')
    parts.append("Ne yapalım?")
    return "\n\n".join(parts)


def build_keyboard(pending: PendingDecision) -> InlineKeyboardMarkup:
    def button(label: str, choice: str) -> list[InlineKeyboardButton]:
        data = DecisionCallback(call_sid=pending.call_sid, nonce=pending.nonce, choice=choice)
        return [InlineKeyboardButton(text=label, callback_data=data.pack())]

    rows = [button(o.label_tr, str(i)) for i, o in enumerate(pending.options)]
    rows += [button(WRITE_LABEL, CHOICE_WRITE), button(REJECT_LABEL, CHOICE_REJECT)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def parse_callback(data: str) -> DecisionCallback | None:
    try:
        return DecisionCallback.unpack(data)
    except (TypeError, ValueError, ValidationError):
        return None


def decision_to_result(decision: OwnerDecision, options: list[OwnerOption]) -> AskOwnerResult:
    """Translate a Telegram decision into the tool result the agent brain understands."""
    if decision.kind is DecisionKind.OPTION:
        option = next(o for o in options if o.id == decision.option_id)
        return AskOwnerResult(decision_text_en=option.meaning_en)
    if decision.kind is DecisionKind.FREE_TEXT:
        return resolve_owner_reply(options, decision.text or "")
    if decision.kind is DecisionKind.REJECTED:
        return AskOwnerResult(decision_text_en=REJECT_MEANING_EN)
    return AskOwnerResult(decision_text_en="", timed_out=True)


class OwnerChannel:
    """Sends decision requests to the owner and resolves them from button clicks or text."""

    def __init__(
        self,
        bot: TelegramBot,
        owner_chat_id: int,
        *,
        free_text_window_seconds: float = FREE_TEXT_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bot = bot
        self.owner_chat_id = owner_chat_id
        self._free_text_window = free_text_window_seconds
        self._clock = clock
        self._pending: dict[str, PendingDecision] = {}
        # (call_sid, nonce) -> short Turkish outcome label.
        self._finished: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._free_text_target: _FreeTextTarget | None = None
        # Set once the app is wired (they need the database, rules and Twilio).
        self.on_command: CommandHandler | None = None
        self.on_forward: ForwardHandler | None = None

    def is_owner(self, user_id: int | None, chat_id: int | None) -> bool:
        return user_id == self.owner_chat_id and chat_id == self.owner_chat_id

    @property
    def pending_call_sids(self) -> list[str]:
        return list(self._pending)

    async def send_decision_request(
        self,
        call_sid: str,
        summary_tr: str,
        options: list[OwnerOption],
        *,
        caller_number: str | None = None,
        caller_said: str | None = None,
    ) -> asyncio.Future[OwnerDecision]:
        """Send one message with a button per option; the Future resolves with the decision."""
        if call_sid in self._pending:
            raise ValueError(f"A decision is already pending for call {short_call_id(call_sid)}")
        pending = PendingDecision(
            call_sid=call_sid,
            nonce=secrets.token_hex(4),
            text=format_request(call_sid, summary_tr, caller_number, caller_said),
            options=options,
            future=asyncio.get_running_loop().create_future(),
        )
        keyboard = build_keyboard(pending)
        # Register first, so a very fast click already finds the request.
        self._pending[call_sid] = pending
        try:
            sent = await asyncio.wait_for(
                self._bot.send_message(self.owner_chat_id, pending.text, reply_markup=keyboard),
                TELEGRAM_TIMEOUT_SECONDS,
            )
        except BaseException:
            self._pending.pop(call_sid, None)
            raise
        pending.message_id = sent.message_id
        return pending.future

    async def ask(
        self,
        call_sid: str,
        summary_tr: str,
        options: list[OwnerOption],
        timeout: float,
        *,
        caller_number: str | None = None,
        caller_said: str | None = None,
    ) -> OwnerDecision:
        """Send a request and wait for it; resolves with TIMEOUT after `timeout` seconds."""
        future = await self.send_decision_request(
            call_sid, summary_tr, options, caller_number=caller_number, caller_said=caller_said
        )
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        except TimeoutError:
            await self._settle(call_sid, OwnerDecision(DecisionKind.TIMEOUT), EXPIRED_TEXT)
            # The owner may have clicked in the same instant; whatever won is the answer.
            return future.result()
        except asyncio.CancelledError:
            # The call ended while on hold. Shielded, so the edit survives the cancellation.
            with suppress(Exception):
                await asyncio.shield(
                    self._settle(call_sid, OwnerDecision(DecisionKind.CANCELLED), HUNG_UP_TEXT)
                )
            raise

    async def handle_callback(
        self, user_id: int | None, chat_id: int | None, data: str, message_id: int | None
    ) -> str | None:
        """Process a button click. Returns the toast text, or None to ignore silently."""
        if not self.is_owner(user_id, chat_id):
            logger.warning("Ignored callback from a non-owner chat")
            return None
        parsed = parse_callback(data)
        if parsed is None:
            logger.warning("Rejected malformed callback data")
            return "Geçersiz düğme."

        pending = self._pending.get(parsed.call_sid)
        if pending is None or pending.nonce != parsed.nonce:
            outcome = self._finished.get((parsed.call_sid, parsed.nonce))
            if outcome is not None:
                return f"Bu arama için karar zaten verildi: {outcome}"
            # Unknown request (expired long ago, or from before a restart): drop the buttons.
            if message_id is not None:
                await self._call_telegram(
                    self._bot.edit_message_reply_markup(
                        chat_id=self.owner_chat_id, message_id=message_id, reply_markup=None
                    )
                )
            return EXPIRED_TEXT

        if parsed.choice == CHOICE_WRITE:
            self._free_text_target = _FreeTextTarget(
                pending.call_sid, pending.nonce, self._clock() + self._free_text_window
            )
            minutes = round(self._free_text_window / 60)
            await self._call_telegram(
                self._bot.send_message(
                    self.owner_chat_id,
                    f"✍️ #{pending.short_id} için cevabınızı yazın ({minutes} dk içinde).",
                )
            )
            return "Cevabınızı yazın."
        if parsed.choice == CHOICE_REJECT:
            await self._settle(
                pending.call_sid, OwnerDecision(DecisionKind.REJECTED), "❌ Reddedildi"
            )
            return "Reddedildi."

        index = int(parsed.choice)
        if index >= len(pending.options):
            return "Geçersiz düğme."
        option = pending.options[index]
        await self._settle(
            pending.call_sid,
            OwnerDecision(DecisionKind.OPTION, option_id=option.id),
            f"✅ Karar: {option.label_tr}",
        )
        return "Karar iletildi."

    async def handle_text(
        self,
        user_id: int | None,
        chat_id: int | None,
        text: str,
        reply_to_message_id: int | None = None,
    ) -> str | None:
        """Process an owner text message. Returns a reply, or None to ignore silently."""
        if not self.is_owner(user_id, chat_id):
            logger.warning("Ignored message from a non-owner chat")
            return None
        text = text.strip()
        if not text:
            return None
        if text.startswith("/"):
            if self.on_command is not None:
                return await self.on_command(text)
            return "Jarvis bağlı. Bekleyen arama sayısı: " + str(len(self._pending))

        pending = self._free_text_recipient(reply_to_message_id)
        if pending is None:
            if not self._pending:
                return "Şu anda bekleyen arama yok."
            return (
                "Birden fazla bekleyen arama var. Önce ilgili mesajda "
                f"“{WRITE_LABEL}” düğmesine basın ya da o mesaja yanıt verin."
            )
        await self._settle(
            pending.call_sid,
            OwnerDecision(DecisionKind.FREE_TEXT, text=text),
            f"✍️ Cevabınız: {text}",
        )
        return f"✅ İletildi (#{pending.short_id})."

    async def send_info(self, text: str, *, silent: bool = False) -> bool:
        """Send a plain message to the owner (summaries, alerts). Never raises.

        `silent` delivers it without a sound (routine call reports).
        """
        return await self._call_telegram(
            self._bot.send_message(self.owner_chat_id, text[:4000], disable_notification=silent)
        )

    async def send_ring_alert(self, call_sid: str, text: str, *, offer_forward: bool) -> bool:
        """An allowlisted caller is ringing: alert now, optionally with a forward button."""
        markup = None
        if offer_forward:
            data = ForwardCallback(call_sid=call_sid).pack()
            markup = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text=FORWARD_LABEL, callback_data=data)]]
            )
        return await self._call_telegram(
            self._bot.send_message(self.owner_chat_id, text[:4000], reply_markup=markup)
        )

    async def handle_forward(
        self, user_id: int | None, chat_id: int | None, data: str, message_id: int | None
    ) -> str | None:
        """The owner pressed "forward to my phone". Returns the toast text."""
        if not self.is_owner(user_id, chat_id):
            logger.warning("Ignored forward callback from a non-owner chat")
            return None
        try:
            parsed = ForwardCallback.unpack(data)
        except (TypeError, ValueError, ValidationError):
            return "Geçersiz düğme."
        if message_id is not None:
            # One press only: drop the button whatever happens next.
            await self._call_telegram(
                self._bot.edit_message_reply_markup(
                    chat_id=self.owner_chat_id, message_id=message_id, reply_markup=None
                )
            )
        if self.on_forward is None:
            return "Yönlendirme ayarlı değil."
        outcome = await self.on_forward(parsed.call_sid)
        await self.send_info(outcome)
        return outcome[:190]

    def _free_text_recipient(self, reply_to_message_id: int | None) -> PendingDecision | None:
        if reply_to_message_id is not None:
            for pending in self._pending.values():
                if pending.message_id == reply_to_message_id:
                    return pending
        target = self._free_text_target
        if target is not None:
            pending = self._pending.get(target.call_sid)
            if (
                pending is not None
                and pending.nonce == target.nonce
                and self._clock() <= target.deadline
            ):
                return pending
            self._free_text_target = None
        if len(self._pending) == 1:
            return next(iter(self._pending.values()))
        return None

    async def _settle(self, call_sid: str, decision: OwnerDecision, outcome: str) -> bool:
        """Resolve a pending request exactly once and show the outcome in Telegram."""
        pending = self._pending.pop(call_sid, None)
        if pending is None or pending.future.done():
            return False
        pending.future.set_result(decision)
        self._finished[(call_sid, pending.nonce)] = outcome
        while len(self._finished) > FINISHED_HISTORY:
            self._finished.popitem(last=False)
        target = self._free_text_target
        if target is not None and target.call_sid == call_sid:
            self._free_text_target = None
        logger.info("Owner decision for call %s: %s", pending.short_id, decision.kind)
        if pending.message_id is not None:
            await self._call_telegram(
                self._bot.edit_message_text(
                    f"{pending.text}\n\n{outcome}",
                    chat_id=self.owner_chat_id,
                    message_id=pending.message_id,
                    reply_markup=None,
                )
            )
        return True

    async def _call_telegram(self, request: Awaitable[Any]) -> bool:
        try:
            await asyncio.wait_for(request, TELEGRAM_TIMEOUT_SECONDS)
        except Exception:
            logger.exception("Telegram request failed")
            return False
        return True


def make_ask_owner_handler(
    channel: OwnerChannel, session: CallSession, timeout: float
) -> Callable[[AskOwnerInput], Awaitable[AskOwnerResult]]:
    """Build the brain's ask_owner tool handler for one call."""

    async def ask_owner(args: AskOwnerInput) -> AskOwnerResult:
        decision = await channel.ask(
            session.call_sid,
            args.summary_tr,
            args.options,
            timeout,
            caller_number=session.caller_number,
            caller_said=args.caller_said_lt,
        )
        return decision_to_result(decision, args.options)

    return ask_owner


def build_router(channel: OwnerChannel) -> Router:
    router = Router(name="owner")
    owner = channel.owner_chat_id
    # Updates from anyone else never reach the handlers.
    router.message.filter(F.chat.id == owner, F.from_user.id == owner)
    router.callback_query.filter(F.from_user.id == owner)

    @router.callback_query(ForwardCallback.filter())
    async def on_forward(query: CallbackQuery) -> None:
        message = query.message
        answer = await channel.handle_forward(
            query.from_user.id,
            message.chat.id if message else None,
            query.data or "",
            message.message_id if message else None,
        )
        if answer is not None:
            await query.answer(answer)

    @router.callback_query()
    async def on_callback(query: CallbackQuery) -> None:
        message = query.message
        answer = await channel.handle_callback(
            query.from_user.id,
            message.chat.id if message else None,
            query.data or "",
            message.message_id if message else None,
        )
        if answer is not None:
            await query.answer(answer)

    @router.message(F.text)
    async def on_text(message: Message) -> None:
        reply_to = message.reply_to_message.message_id if message.reply_to_message else None
        reply = await channel.handle_text(
            message.from_user.id if message.from_user else None,
            message.chat.id,
            message.text or "",
            reply_to,
        )
        if reply is not None:
            await message.answer(reply)

    return router


@dataclass
class TelegramRuntime:
    """A running bot: long polling in a background task, plus the owner channel."""

    bot: Bot
    dispatcher: Dispatcher
    channel: OwnerChannel
    task: asyncio.Task[None] = field(repr=False)

    async def stop(self) -> None:
        with suppress(RuntimeError):
            await self.dispatcher.stop_polling()
        self.task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await self.task
        await self.bot.session.close()


async def start_telegram(settings: Settings) -> TelegramRuntime | None:
    """Start long polling if the bot is configured; returns None (and logs) otherwise."""
    if settings.telegram_bot_token is None or settings.telegram_owner_chat_id is None:
        logger.warning("Telegram not configured; owner channel disabled")
        return None
    bot = Bot(settings.telegram_bot_token.get_secret_value())
    channel = OwnerChannel(bot, settings.telegram_owner_chat_id)
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(channel))
    try:
        await asyncio.wait_for(
            bot.set_my_commands(
                [BotCommand(command=name, description=desc) for name, desc in BOT_COMMANDS]
            ),
            TELEGRAM_TIMEOUT_SECONDS,
        )
    except Exception:
        # Only the menu is missing; typed commands still work.
        logger.warning("Could not register the Telegram command menu", exc_info=True)
    task = asyncio.create_task(
        dispatcher.start_polling(
            bot,
            handle_signals=False,
            close_bot_session=False,
            allowed_updates=["message", "callback_query"],
        ),
        name="telegram-polling",
    )
    logger.info("Telegram owner channel started (long polling)")
    return TelegramRuntime(bot, dispatcher, channel, task)
