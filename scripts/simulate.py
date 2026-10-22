"""Text-only call simulator: you type as the caller, and as the owner when ask_owner fires.

Usage:
    python scripts/simulate.py [--caller-number +37060000000] [--rules rules.yaml] [--telegram]

With --telegram, ask_owner goes to your Telegram bot (buttons / free text) instead of the
terminal, and saved messages, suspicious-call alerts and a post-call summary are sent there too.
Do not run it while the FastAPI app is polling the same bot.

Type /quit (or Ctrl-D) to hang up.
"""

import argparse
import asyncio
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.brain import create_brain  # noqa: E402
from app.agent.tools import (  # noqa: E402
    AskOwnerInput,
    AskOwnerResult,
    EndCallInput,
    FlagSuspiciousInput,
    SaveMessageInput,
    ToolHandlers,
    resolve_owner_reply,
)
from app.config import get_settings  # noqa: E402
from app.models import CallSession, CallStatus  # noqa: E402
from app.owner_channel import (  # noqa: E402
    OwnerChannel,
    make_ask_owner_handler,
    short_call_id,
    start_telegram,
)
from app.rules import RulesEngine  # noqa: E402

DIM, BOLD, YELLOW, RESET = "\033[2m", "\033[1m", "\033[33m", "\033[0m"


async def read_line(prompt: str) -> str | None:
    try:
        return await asyncio.to_thread(input, prompt)
    except EOFError:
        return None


def echo_if_piped(text: str | None) -> None:
    # When stdin is a pipe, input() does not echo, so print what was "typed".
    if text is not None and not sys.stdin.isatty():
        print(text)


async def ask_owner(args: AskOwnerInput) -> AskOwnerResult:
    print(f"\n{YELLOW}{BOLD}--- Telegram (owner) ---{RESET}")
    print(f"{YELLOW}{args.summary_tr}{RESET}")
    print(f'{YELLOW}Arayan: "{args.caller_said_lt}"{RESET}')
    for i, option in enumerate(args.options, start=1):
        print(f"{YELLOW}  [{i}] {option.label_tr}  {DIM}({option.id}: {option.meaning_en}){RESET}")
    reply = await read_line(f"{YELLOW}Owner reply (number, id, or free text)> {RESET}")
    echo_if_piped(reply)
    print(f"{YELLOW}{BOLD}------------------------{RESET}\n")
    return resolve_owner_reply(args.options, reply or "")


async def save_message(args: SaveMessageInput) -> None:
    print(f"{DIM}[save_message] {args.model_dump(exclude_none=True)}{RESET}")


async def end_call(args: EndCallInput) -> None:
    print(f"{DIM}[end_call] {args.reason}{RESET}")


async def flag_suspicious(args: FlagSuspiciousInput) -> None:
    print(f"{DIM}[flag_suspicious] {args.reason}{RESET}")


def telegram_handlers(
    channel: OwnerChannel, session: CallSession, hold_timeout: float
) -> ToolHandlers:
    tag = f"#{short_call_id(session.call_sid)} · {session.caller_number}"
    ask = make_ask_owner_handler(channel, session, hold_timeout)

    async def ask_owner_telegram(args: AskOwnerInput) -> AskOwnerResult:
        print(f"{YELLOW}[ask_owner] sent to Telegram, waiting up to {hold_timeout:.0f}s...{RESET}")
        result = await ask(args)
        outcome = "TIMEOUT" if result.timed_out else result.decision_text_en
        print(f"{YELLOW}[ask_owner] {outcome}{RESET}")
        return result

    async def save_message_telegram(args: SaveMessageInput) -> None:
        await save_message(args)
        lines = [f"📝 Mesaj {tag}"]
        lines += [f"{k}: {v}" for k, v in args.model_dump(exclude_none=True).items()]
        await channel.send_info("\n".join(lines))

    async def flag_suspicious_telegram(args: FlagSuspiciousInput) -> None:
        await flag_suspicious(args)
        await channel.send_info(f"⚠️ Şüpheli arama {tag}\n{args.reason}")

    return ToolHandlers(
        ask_owner_telegram, save_message_telegram, end_call, flag_suspicious_telegram
    )


def call_summary(session: CallSession) -> str:
    lines = [f"📴 Arama bitti #{short_call_id(session.call_sid)} · {session.caller_number}"]
    if session.owner_decision:
        lines.append(f"Karar: {session.owner_decision}")
    lines.append(f"Konuşma: {len(session.transcript)} satır")
    return "\n".join(lines)


async def speak(replies) -> None:  # type: ignore[no-untyped-def]
    async for sentence in replies:
        print(f"{BOLD}Agent:{RESET} {sentence}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--caller-number", default="+37060000000")
    parser.add_argument("--rules", default=None, help="defaults to RULES_PATH, then the example")
    parser.add_argument("--telegram", action="store_true", help="ask the owner via Telegram")
    args = parser.parse_args()

    settings = get_settings()
    if settings.anthropic_api_key is None:
        print("ANTHROPIC_API_KEY is not set. Add it to .env (see README) and retry.")
        return 2

    rules_path = Path(args.rules or settings.rules_path)
    if not rules_path.exists():
        rules_path = Path("rules.example.yaml")
    rules = RulesEngine.from_yaml(rules_path)
    print(f"{DIM}model={settings.anthropic_model} rules={rules_path} caller={args.caller_number}")
    print(f"hold timeout={settings.hold_timeout_seconds:.0f}s, /quit to hang up{RESET}\n")

    session = CallSession(call_sid=f"SIM{secrets.token_hex(4)}", caller_number=args.caller_number)
    session.status = CallStatus.IN_PROGRESS
    telegram = None
    if args.telegram:
        telegram = await start_telegram(settings)
        if telegram is None:
            print("TELEGRAM_BOT_TOKEN / TELEGRAM_OWNER_CHAT_ID missing in .env.")
            print("Run: python scripts/check_telegram.py")
            return 2
        handlers = telegram_handlers(telegram.channel, session, settings.hold_timeout_seconds)
        print(f"{DIM}owner channel: Telegram (call #{short_call_id(session.call_sid)}){RESET}")
    else:
        handlers = ToolHandlers(ask_owner, save_message, end_call, flag_suspicious)
    brain = create_brain(settings, handlers, rules, args.caller_number, session)

    try:
        await speak(brain.greet())
        while not brain.ended:
            line = await read_line("Caller> ")
            echo_if_piped(line)
            if line is None or line.strip() == "/quit":
                break
            await speak(brain.respond(line))
        session.status = CallStatus.COMPLETED
        print(f"\n{DIM}Call over ({len(session.transcript)} transcript turns).{RESET}")
        if telegram is not None:
            await telegram.channel.send_info(call_summary(session))
    finally:
        if telegram is not None:
            await telegram.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
