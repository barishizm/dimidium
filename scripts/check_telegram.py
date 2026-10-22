"""Check the Telegram bot setup in .env.

Usage: python scripts/check_telegram.py

1. Verifies TELEGRAM_BOT_TOKEN by asking Telegram who the bot is.
2. If TELEGRAM_OWNER_CHAT_ID is empty: lists the chat IDs of recent messages sent to the bot
   (send your bot any message first, e.g. /start).
3. If it is set: sends a test message to that chat.

Do not run it while the app or the simulator is polling the same bot.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram import Bot  # noqa: E402
from aiogram.exceptions import (  # noqa: E402
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramUnauthorizedError,
)
from aiogram.utils.token import TokenValidationError  # noqa: E402

from app.config import get_settings  # noqa: E402

TIMEOUT = 15


async def main() -> int:
    settings = get_settings()
    if settings.telegram_bot_token is None:
        print("FAIL: TELEGRAM_BOT_TOKEN is empty in .env (get it from @BotFather).")
        return 1
    try:
        bot = Bot(settings.telegram_bot_token.get_secret_value())
    except TokenValidationError:
        print("FAIL: TELEGRAM_BOT_TOKEN does not look like a bot token (123456:ABC...).")
        return 1
    try:
        return await check(bot, settings.telegram_owner_chat_id)
    except TelegramUnauthorizedError:
        print("FAIL: Telegram rejected the token. Copy it again from @BotFather.")
        return 1
    except TelegramNetworkError:
        print("FAIL: could not reach api.telegram.org (network or timeout).")
        return 1
    finally:
        await bot.session.close()


async def check(bot: Bot, owner_chat_id: int | None) -> int:
    me = await bot.get_me(request_timeout=TIMEOUT)
    print(f"OK: token belongs to @{me.username} ({me.first_name}).")

    if owner_chat_id is None:
        updates = await bot.get_updates(timeout=0, request_timeout=TIMEOUT)
        chats = {
            u.message.chat.id: u.message.from_user.full_name if u.message.from_user else "?"
            for u in updates
            if u.message and u.message.chat.type == "private"
        }
        if not chats:
            print(f"No messages yet. Open https://t.me/{me.username}, press Start, then rerun.")
            return 1
        for chat_id, name in chats.items():
            print(f"Found private chat: {chat_id}  ({name})")
        print("Put YOUR number into .env as TELEGRAM_OWNER_CHAT_ID=..., then rerun to test.")
        return 0

    try:
        await bot.send_message(
            owner_chat_id,
            "✅ Jarvis test mesajı. Bu sohbet artık sahip kanalı.",
            request_timeout=TIMEOUT,
        )
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        print(f"FAIL: could not message chat {owner_chat_id}: {exc.message}")
        print("Check the chat ID and make sure you pressed Start in the bot chat.")
        return 1
    print(f"OK: test message sent to chat {owner_chat_id}. Check Telegram.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
