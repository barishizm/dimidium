"""Check the whole setup and print a pass/fail table.

Usage:
    python scripts/doctor.py            # everything (free API calls, no message sent, no call)
    python scripts/doctor.py --offline  # only the .env checks and the local signature test

Checks (docs/SETUP_TELNYX.md):
- .env: required variables, OPENAI_PROJECT_ID / TELNYX_NUMBER format, public https URL, rules.
- OpenAI: the key works, the models are available, the key belongs to OPENAI_PROJECT_ID, and the
  GPT-Live call-control endpoint answers this key (a reject for a made-up session ID).
- Webhook signature: a payload signed locally with OPENAI_WEBHOOK_SECRET verifies with the
  OpenAI SDK (offline), then PUBLIC_BASE_URL/openai/webhook accepts a signed test event and
  refuses a badly signed one (proves the URL routes to this app and the secrets match).
- Public URL: PUBLIC_BASE_URL/health answers from the internet.
- Telegram: the bot token works and the bot can reach TELEGRAM_OWNER_CHAT_ID (no message sent).
- Telnyx (only with TELNYX_API_KEY, read-only): TELNYX_NUMBER exists and is active, its
  connection is an FQDN connection to sip.api.openai.com:5061 with TLS, SRTP and usable codecs,
  the number's Translated Number is OPENAI_PROJECT_ID, and call recording is off.

What it cannot check: that GPT-Live SIP is enabled for the OpenAI project (no API shows it)
and that audio flows both ways. A real test call proves those (docs/SETUP_TELNYX.md, step 8).

Exit code 1 if anything FAILs. Run it on the server too: `fly ssh console -C
"python scripts/doctor.py"`, Render Shell, or `docker compose exec app python scripts/doctor.py`.
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings, get_settings  # noqa: E402
from app.live.setup_checks import (  # noqa: E402
    Finding,
    audit_call_recording,
    audit_connection,
    audit_number,
    audit_translated_number,
    signed_headers,
    valid_e164,
    valid_project_id,
)

OPENAI_API = "https://api.openai.com/v1"
TELNYX_API = "https://api.telnyx.com/v2"
CHECK_TIMEOUT = 25
# A session ID that cannot exist: OpenAI answers session_id_not_found, nothing is rejected.
PROBE_SESSION_ID = "live_sess_doctor_probe"


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

    def add_finding(self, finding: Finding) -> None:
        self.add(finding.check, finding.status, finding.detail)

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
            "TELEGRAM_BOT_TOKEN": s.telegram_bot_token,
            "TELEGRAM_OWNER_CHAT_ID": s.telegram_owner_chat_id,
        }
        for name, value in required.items():
            if value is None or value == "":
                self.add(f"env {name}", "FAIL", "missing (see .env.example)")
            else:
                self.add(f"env {name}", "PASS", "set")

        if s.openai_project_id is None:
            self.add(
                "env OPENAI_PROJECT_ID",
                "FAIL",
                "missing (platform.openai.com > Settings > General)",
            )
        elif not valid_project_id(s.openai_project_id):
            self.add(
                "env OPENAI_PROJECT_ID",
                "FAIL",
                f"{s.openai_project_id!r} is not a proj_... ID (not the name, not an org_ ID)",
            )
        else:
            self.add("env OPENAI_PROJECT_ID", "PASS", s.openai_project_id)

        secret = s.openai_webhook_secret
        if secret is not None and not secret.get_secret_value().startswith("whsec_"):
            self.add(
                "env OPENAI_WEBHOOK_SECRET", "WARN", "does not start with whsec_: right value?"
            )

        url = s.public_base_url
        if not url.startswith("https://"):
            self.add("env PUBLIC_BASE_URL", "FAIL", f"must be the public https URL, is {url}")
        else:
            self.add("env PUBLIC_BASE_URL", "PASS", url)

        if s.telnyx_number is None:
            self.add("env TELNYX_NUMBER", "WARN", "not set: no loop check, no Telnyx number check")
        elif not valid_e164(s.telnyx_number):
            self.add("env TELNYX_NUMBER", "FAIL", f"{s.telnyx_number!r} is not E.164 (+370...)")
        else:
            self.add("env TELNYX_NUMBER", "PASS", s.telnyx_number)

        if s.telnyx_api_key is None:
            self.add(
                "env TELNYX_API_KEY", "SKIP", "optional: without it the Telnyx checks are skipped"
            )
        else:
            self.add("env TELNYX_API_KEY", "PASS", "set")

        if Path(s.rules_path).is_file():
            self.add("env RULES_PATH", "PASS", s.rules_path)
        else:
            self.add("env RULES_PATH", "WARN", f"{s.rules_path} not found: empty rules")

        if not s.private_term_list and not s.owner_phone_number:
            self.add("env PRIVATE_TERMS", "WARN", "empty: no code check for your address or number")

    # --- webhook signature, local ---------------------------------------------------------

    def check_signature_local(self) -> None:
        """Sign a payload here and verify it with the SDK: the secret has a usable format."""
        secret = self.settings.openai_webhook_secret
        if secret is None:
            self.add("Signature (local)", "SKIP", "no OPENAI_WEBHOOK_SECRET")
            return
        from openai import InvalidWebhookSignatureError, OpenAI

        value = secret.get_secret_value()
        body = json.dumps({"id": "evt_selftest", "object": "event", "type": "dimidium.selftest"})
        try:
            client = OpenAI(api_key="unused-offline", webhook_secret=value)
            client.webhooks.verify_signature(body, signed_headers(value, body), secret=value)
        except Exception as exc:  # noqa: BLE001 - e.g. a secret that is not valid base64
            self.add(
                "Signature (local)", "FAIL", f"{type(exc).__name__}: secret unusable? {exc}"[:160]
            )
            return
        self.add("Signature (local)", "PASS", "a locally signed test payload verifies")
        try:
            client.webhooks.verify_signature(
                body, {**signed_headers(value, body), "webhook-signature": "v1,AAAA"}, secret=value
            )
        except InvalidWebhookSignatureError:
            self.add("Signature (bad, local)", "PASS", "a wrong signature is refused")
        else:
            self.add("Signature (bad, local)", "FAIL", "a wrong signature was accepted")

    # --- network: OpenAI ------------------------------------------------------------------

    async def check_openai(self) -> None:
        s = self.settings
        if s.openai_api_key is None:
            self.add("OpenAI API", "SKIP", "no OPENAI_API_KEY")
            return
        headers = {"Authorization": f"Bearer {s.openai_api_key.get_secret_value()}"}
        async with httpx.AsyncClient(headers=headers, timeout=CHECK_TIMEOUT) as client:
            project_header: str | None = None
            for model in (s.live_voice_model, s.live_backend_model):
                response = await client.get(f"{OPENAI_API}/models/{model}")
                if response.status_code == 401:
                    self.add("OpenAI API", "FAIL", "key rejected (401)")
                    return
                if response.status_code == 200:
                    self.add(f"OpenAI {model}", "PASS", "key works, model available")
                    project_header = project_header or response.headers.get("openai-project")
                else:
                    self.add(
                        f"OpenAI {model}",
                        "FAIL",
                        f"HTTP {response.status_code}: check billing and model access",
                    )
            self._check_key_project(project_header)
            await self._probe_call_control(client)

    def _check_key_project(self, actual: str | None) -> None:
        """The key's project, from the `openai-project` response header (when OpenAI sends it)."""
        expected = self.settings.openai_project_id
        if not expected:
            return
        if actual is None:
            self.add(
                "OpenAI key project",
                "WARN",
                "OpenAI did not say which project the key belongs to: check it is the project of "
                "OPENAI_PROJECT_ID (key, webhook and Translated Number must match)",
            )
        elif actual == expected:
            self.add("OpenAI key project", "PASS", f"key belongs to {actual}")
        else:
            self.add(
                "OpenAI key project",
                "FAIL",
                f"key belongs to {actual} but OPENAI_PROJECT_ID is {expected}: "
                "accept will fail with session_id_not_found",
            )

    async def _probe_call_control(self, client: httpx.AsyncClient) -> None:
        """Reject a session that does not exist: shows the key can reach GPT-Live call control."""
        response = await client.post(
            f"{OPENAI_API}/live/sessions/{PROBE_SESSION_ID}/reject", json={"status_code": 486}
        )
        code = ""
        try:
            error = response.json().get("error")
            code = (error or {}).get("code") or "" if isinstance(error, dict) else ""
        except ValueError:
            pass
        if response.status_code == 404 or code == "session_id_not_found":
            self.add(
                "GPT-Live call control",
                "PASS",
                "endpoint reachable with this key (SIP enablement shows only on a real call)",
            )
        elif response.status_code == 401:
            self.add("GPT-Live call control", "FAIL", "key rejected (401)")
        elif response.status_code == 403:
            self.add(
                "GPT-Live call control",
                "FAIL",
                "403: GPT-Live SIP may not be enabled for this project; ask OpenAI support to "
                "enable it (give them OPENAI_PROJECT_ID)",
            )
        else:
            self.add(
                "GPT-Live call control",
                "WARN",
                f"unexpected HTTP {response.status_code} {code}".strip(),
            )

    # --- network: Telegram ----------------------------------------------------------------

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

    # --- network: Telnyx ------------------------------------------------------------------

    async def check_telnyx(self) -> None:
        s = self.settings
        if s.telnyx_api_key is None:
            self.add("Telnyx account", "SKIP", "no TELNYX_API_KEY (optional)")
            return
        if not s.telnyx_number:
            self.add("Telnyx number", "SKIP", "no TELNYX_NUMBER")
            return
        headers = {"Authorization": f"Bearer {s.telnyx_api_key.get_secret_value()}"}
        async with httpx.AsyncClient(headers=headers, timeout=CHECK_TIMEOUT) as client:
            response = await client.get(
                f"{TELNYX_API}/phone_numbers",
                params={"filter[phone_number]": s.telnyx_number, "page[size]": 25},
            )
            if response.status_code in (401, 403):
                self.add(
                    "Telnyx account", "FAIL", f"API key rejected (HTTP {response.status_code})"
                )
                return
            response.raise_for_status()
            self.add("Telnyx account", "PASS", "API key works")
            record, finding = audit_number(response.json().get("data", []), s.telnyx_number)
            self.add_finding(finding)
            if record is None or not record.get("connection_id"):
                return

            connection_id = record["connection_id"]
            connection = await client.get(f"{TELNYX_API}/fqdn_connections/{connection_id}")
            if connection.status_code == 404:
                self.add(
                    "Telnyx connection",
                    "FAIL",
                    "the number's connection is not an FQDN connection: create one "
                    "(docs/SETUP_TELNYX.md step 2) and assign the number to it",
                )
                return
            connection.raise_for_status()
            fqdns = await client.get(
                f"{TELNYX_API}/fqdns", params={"filter[connection_id]": connection_id}
            )
            fqdns.raise_for_status()
            for item in audit_connection(connection.json()["data"], fqdns.json().get("data", [])):
                self.add_finding(item)

            voice = await client.get(f"{TELNYX_API}/phone_numbers/{record['id']}/voice")
            voice.raise_for_status()
            voice_data = voice.json()["data"]
            self.add_finding(audit_translated_number(voice_data, s.openai_project_id))
            self.add_finding(audit_call_recording(voice_data))

    # --- network: our own public URL ------------------------------------------------------

    async def check_public_url(self) -> None:
        base = self.settings.public_base_url.rstrip("/")
        if not base.startswith("https://"):
            self.add("Public URL /health", "SKIP", "PUBLIC_BASE_URL is not https")
            return
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            try:
                response = await client.get(f"{base}/health")
            except httpx.HTTPError as exc:
                self.add("Public URL /health", "FAIL", f"unreachable: {type(exc).__name__}")
                return
            if response.status_code != 200:
                self.add(
                    "Public URL /health",
                    "FAIL",
                    f"HTTP {response.status_code}: is the app running and the tunnel/domain up?",
                )
                return
            self.add("Public URL /health", "PASS", "the app answers from the internet")
            await self._check_webhook_route(client, base)

    async def _check_webhook_route(self, client: httpx.AsyncClient, base: str) -> None:
        secret = self.settings.openai_webhook_secret
        if secret is None:
            self.add("Webhook route", "SKIP", "no OPENAI_WEBHOOK_SECRET")
            return
        url = f"{base}/openai/webhook"
        body = json.dumps({"id": "evt_selftest", "object": "event", "type": "dimidium.selftest"})
        headers = signed_headers(secret.get_secret_value(), body)
        good = await client.post(url, content=body, headers=headers)
        bad = await client.post(
            url, content=body, headers={**headers, "webhook-signature": "v1,AAAA"}
        )
        if good.status_code == 200:
            self.add("Webhook signed", "PASS", f"signed test event accepted at {url}")
        elif good.status_code == 503:
            self.add(
                "Webhook signed",
                "FAIL",
                "503: the running app lacks OPENAI_API_KEY / OPENAI_WEBHOOK_SECRET",
            )
        else:
            self.add(
                "Webhook signed",
                "FAIL",
                f"HTTP {good.status_code}: the running app has a different OPENAI_WEBHOOK_SECRET, "
                "or the server clock is off by more than 5 minutes",
            )
        if bad.status_code == 400:
            self.add("Webhook bad signature", "PASS", "refused (400)")
        else:
            self.add("Webhook bad signature", "FAIL", f"HTTP {bad.status_code}, want 400")


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
    parser.add_argument(
        "--offline", action="store_true", help="only check .env and the local signature, no network"
    )
    args = parser.parse_args()

    doctor = Doctor(get_settings())
    doctor.check_env()
    doctor.check_signature_local()
    if not args.offline:
        await doctor.run("OpenAI API", doctor.check_openai)
        await doctor.run("Public URL /health", doctor.check_public_url)
        await doctor.run("Telegram bot", doctor.check_telegram)
        await doctor.run("Telnyx account", doctor.check_telnyx)
    print_table(doctor.results)
    return 1 if any(r.status == "FAIL" for r in doctor.results) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
