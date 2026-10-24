"""Check the whole setup and print a pass/fail table.

Usage:
    python scripts/doctor.py            # everything (free API calls, no message sent)
    python scripts/doctor.py --offline  # only the .env checks, no network

Checks:
- .env: required variables, public https URL, rules file.
- OpenAI: the key works and the project can use LIVE_VOICE_MODEL and LIVE_BACKEND_MODEL
  (models endpoint, free).
- Telegram: the bot token works and the bot can reach TELEGRAM_OWNER_CHAT_ID (no message sent).
- Twilio: credentials, the number is on the account, trial status.
- Public URL: PUBLIC_BASE_URL/health answers (the app is reachable from the internet).

The SIP path in detail (trunk, origination URI, signed test webhook): scripts/check_sip.py.

Exit code 1 if anything FAILs. Run it on the server too: `fly ssh console -C
"python scripts/doctor.py"`, Render Shell, or `docker compose exec app python scripts/doctor.py`.
"""

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings, get_settings  # noqa: E402

OPENAI_API = "https://api.openai.com/v1"
TWILIO_API = "https://api.twilio.com/2010-04-01"
CHECK_TIMEOUT = 25


@dataclass
class Result:
    check: str
    status: str  # PASS | FAIL | WARN | SKIP
    detail: str


class Doctor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.results: list[Result] = []

    def add(self, check: str, status: str, detail: str) -> None:
        self.results.append(Result(check, status, detail))

    async def run(self, check: str, probe: Callable[[], Awaitable[None]]) -> None:
        """Run a network probe; a timeout or unexpected error is a FAIL, never a crash."""
        try:
            await asyncio.wait_for(probe(), CHECK_TIMEOUT)
        except TimeoutError:
            self.add(check, "FAIL", f"no answer within {CHECK_TIMEOUT} s")
        except Exception as exc:  # noqa: BLE001 - report it in the table and go on
            self.add(check, "FAIL", f"{type(exc).__name__}: {exc}"[:160])

    # --- .env ----------------------------------------------------------------------------

    def check_env(self) -> None:
        s = self.settings
        required = {
            "OPENAI_API_KEY": s.openai_api_key,
            "OPENAI_WEBHOOK_SECRET": s.openai_webhook_secret,
            "OPENAI_PROJECT_ID": s.openai_project_id,
            "TELEGRAM_BOT_TOKEN": s.telegram_bot_token,
            "TELEGRAM_OWNER_CHAT_ID": s.telegram_owner_chat_id,
            "TWILIO_ACCOUNT_SID": s.twilio_account_sid,
            "TWILIO_AUTH_TOKEN": s.twilio_auth_token,
            "TWILIO_PHONE_NUMBER": s.twilio_phone_number,
        }
        for name, value in required.items():
            if value is None or value == "":
                self.add(f"env {name}", "FAIL", "missing (see .env.example)")
            else:
                self.add(f"env {name}", "PASS", "set")

        url = s.public_base_url
        if not url.startswith("https://"):
            self.add("env PUBLIC_BASE_URL", "FAIL", f"must be the public https URL, is {url}")
        else:
            self.add("env PUBLIC_BASE_URL", "PASS", url)

        if Path(s.rules_path).is_file():
            self.add("env RULES_PATH", "PASS", s.rules_path)
        else:
            self.add("env RULES_PATH", "WARN", f"{s.rules_path} not found: empty rules")

        if not s.private_term_list and not s.owner_phone_number:
            self.add("env PRIVATE_TERMS", "WARN", "empty: no code check for your address or number")

    # --- network -------------------------------------------------------------------------

    async def check_openai(self) -> None:
        s = self.settings
        if s.openai_api_key is None:
            self.add("OpenAI API", "SKIP", "no OPENAI_API_KEY")
            return
        headers = {"Authorization": f"Bearer {s.openai_api_key.get_secret_value()}"}
        async with httpx.AsyncClient(headers=headers, timeout=CHECK_TIMEOUT) as client:
            for model in (s.live_voice_model, s.live_backend_model):
                response = await client.get(f"{OPENAI_API}/models/{model}")
                if response.status_code == 401:
                    self.add("OpenAI API", "FAIL", "key rejected (401)")
                    return
                if response.status_code == 200:
                    self.add(f"OpenAI {model}", "PASS", "key works, model available")
                else:
                    self.add(
                        f"OpenAI {model}",
                        "FAIL",
                        f"HTTP {response.status_code}: check billing and model access",
                    )

    async def check_telegram(self) -> None:
        s = self.settings
        if s.telegram_bot_token is None:
            self.add("Telegram bot", "SKIP", "no TELEGRAM_BOT_TOKEN")
            return
        from aiogram import Bot
        from aiogram.exceptions import (
            TelegramBadRequest,
            TelegramForbiddenError,
            TelegramUnauthorizedError,
        )
        from aiogram.utils.token import TokenValidationError

        try:
            bot = Bot(s.telegram_bot_token.get_secret_value())
        except TokenValidationError:
            self.add("Telegram bot", "FAIL", "token does not look like 123456:ABC...")
            return
        try:
            me = await bot.get_me(request_timeout=CHECK_TIMEOUT)
            self.add("Telegram bot", "PASS", f"@{me.username}")
            if s.telegram_owner_chat_id is None:
                self.add("Telegram owner chat", "SKIP", "no TELEGRAM_OWNER_CHAT_ID")
                return
            try:
                chat = await bot.get_chat(s.telegram_owner_chat_id, request_timeout=CHECK_TIMEOUT)
            except (TelegramBadRequest, TelegramForbiddenError) as exc:
                self.add(
                    "Telegram owner chat",
                    "FAIL",
                    f"{exc.message}: press Start in the bot chat, check the ID",
                )
                return
            if chat.type != "private":
                self.add("Telegram owner chat", "WARN", f"chat is a {chat.type}, not private")
            else:
                self.add("Telegram owner chat", "PASS", f"reachable ({chat.first_name or '?'})")
        except TelegramUnauthorizedError:
            self.add("Telegram bot", "FAIL", "token rejected by Telegram")
        finally:
            await bot.session.close()

    async def check_twilio(self) -> None:
        s = self.settings
        if not s.twilio_account_sid or s.twilio_auth_token is None:
            self.add("Twilio account", "SKIP", "no TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN")
            return
        sid = s.twilio_account_sid
        auth = (sid, s.twilio_auth_token.get_secret_value())
        async with httpx.AsyncClient(auth=auth, timeout=CHECK_TIMEOUT) as client:
            account = await client.get(f"{TWILIO_API}/Accounts/{sid}.json")
            if account.status_code != 200:
                self.add(
                    "Twilio account", "FAIL", f"credentials rejected (HTTP {account.status_code})"
                )
                return
            info = account.json()
            if info.get("status") != "active":
                self.add("Twilio account", "FAIL", f"account status is {info.get('status')}")
            elif info.get("type") == "Trial":
                self.add("Twilio account", "WARN", "trial: only verified numbers can call in")
            else:
                self.add(
                    "Twilio account", "PASS", f"{info.get('friendly_name')} ({info.get('type')})"
                )

            if not s.twilio_phone_number:
                self.add("Twilio number", "SKIP", "no TWILIO_PHONE_NUMBER")
                return
            response = await client.get(
                f"{TWILIO_API}/Accounts/{sid}/IncomingPhoneNumbers.json",
                params={"PhoneNumber": s.twilio_phone_number},
            )
            response.raise_for_status()
            numbers = response.json().get("incoming_phone_numbers", [])
            if not numbers:
                self.add("Twilio number", "FAIL", f"{s.twilio_phone_number} is not on this account")
                return
            number = numbers[0]
            self.add("Twilio number", "PASS", number["phone_number"])

    async def check_public_url(self) -> None:
        url = self.settings.public_base_url.rstrip("/") + "/health"
        if not url.startswith("https://"):
            self.add("Public URL /health", "SKIP", "PUBLIC_BASE_URL is not https")
            return
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            try:
                response = await client.get(url)
            except httpx.HTTPError as exc:
                self.add("Public URL /health", "FAIL", f"unreachable: {type(exc).__name__}")
                return
        if response.status_code == 200:
            self.add("Public URL /health", "PASS", "the app answers from the internet")
        else:
            self.add(
                "Public URL /health",
                "FAIL",
                f"HTTP {response.status_code}: is the app running and the tunnel/domain up?",
            )


def print_table(results: list[Result]) -> None:
    width = max(len(r.check) for r in results)
    print(f"{'CHECK'.ljust(width)}  RESULT  DETAILS")
    print(f"{'-' * width}  ------  {'-' * 40}")
    for r in results:
        print(f"{r.check.ljust(width)}  {r.status.ljust(6)}  {r.detail}")
    counts = {
        status: sum(r.status == status for r in results)
        for status in ("PASS", "WARN", "FAIL", "SKIP")
    }
    print("\n" + ", ".join(f"{n} {status}" for status, n in counts.items() if n))


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--offline", action="store_true", help="only check .env, no network")
    args = parser.parse_args()

    doctor = Doctor(get_settings())
    doctor.check_env()
    if not args.offline:
        await doctor.run("OpenAI API", doctor.check_openai)
        await doctor.run("Telegram bot", doctor.check_telegram)
        await doctor.run("Twilio account", doctor.check_twilio)
        await doctor.run("Public URL /health", doctor.check_public_url)
    print_table(doctor.results)
    return 1 if any(r.status == "FAIL" for r in doctor.results) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
