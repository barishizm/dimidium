"""Check the Twilio setup for live calls, and optionally point the number at this app.

Usage:
    python scripts/check_twilio.py                 # read-only checks
    python scripts/check_twilio.py --set-webhook   # also set the number's voice webhook

Checks: TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN work, TWILIO_PHONE_NUMBER is on the account,
its "A call comes in" webhook is PUBLIC_BASE_URL/twilio/voice (HTTP POST), and the app answers
on PUBLIC_BASE_URL/health through the tunnel.
"""

import argparse
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402

API = "https://api.twilio.com/2010-04-01"
OK, FAIL, WARN = "✅", "❌", "⚠️ "


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set-webhook", action="store_true", help="set the voice webhook")
    args = parser.parse_args()
    settings = get_settings()

    sid, token = settings.twilio_account_sid, settings.twilio_auth_token
    if not sid or token is None:
        print(f"{FAIL} TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN are not set in .env")
        return 2
    auth = (sid, token.get_secret_value())
    webhook = settings.public_base_url.rstrip("/") + "/twilio/voice"
    problems = 0

    with httpx.Client(auth=auth, timeout=15) as client:
        account = client.get(f"{API}/Accounts/{sid}.json")
        if account.status_code != 200:
            print(f"{FAIL} Twilio rejected the credentials (HTTP {account.status_code})")
            return 1
        info = account.json()
        print(f"{OK} Credentials work: account '{info.get('friendly_name')}' ({info.get('type')})")
        if info.get("type") == "Trial":
            print(f"{WARN} Trial account: callers hear a trial notice first, and only verified")
            print("   numbers (Console > Phone Numbers > Verified Caller IDs) can call in.")

        if not settings.twilio_phone_number:
            print(f"{FAIL} TWILIO_PHONE_NUMBER is not set (E.164, e.g. +3706XXXXXXX)")
            return 1
        numbers = (
            client.get(
                f"{API}/Accounts/{sid}/IncomingPhoneNumbers.json",
                params={"PhoneNumber": settings.twilio_phone_number},
            )
            .json()
            .get("incoming_phone_numbers", [])
        )
        if not numbers:
            print(f"{FAIL} {settings.twilio_phone_number} is not a number on this account")
            return 1
        number = numbers[0]
        print(f"{OK} Number {number['phone_number']} found")

        current = number.get("voice_url") or "(none)"
        method = number.get("voice_method", "")
        if current == webhook and method == "POST":
            print(f"{OK} Voice webhook: POST {current}")
        elif args.set_webhook:
            client.post(
                f"{API}/Accounts/{sid}/IncomingPhoneNumbers/{number['sid']}.json",
                data={"VoiceUrl": webhook, "VoiceMethod": "POST"},
            ).raise_for_status()
            print(f"{OK} Voice webhook set: POST {webhook} (was {method} {current})")
        else:
            problems += 1
            print(f"{FAIL} Voice webhook is {method} {current}")
            print(f"   expected POST {webhook}  (fix: rerun with --set-webhook)")

    if not settings.public_base_url.startswith("https://"):
        problems += 1
        print(
            f"{FAIL} PUBLIC_BASE_URL must be the public https URL (ngrok), "
            f"not {settings.public_base_url}"
        )
    else:
        try:
            health = httpx.get(settings.public_base_url.rstrip("/") + "/health", timeout=10)
            if health.status_code == 200:
                print(f"{OK} The app answers through {settings.public_base_url}")
            else:
                problems += 1
                print(
                    f"{FAIL} {settings.public_base_url}/health returned HTTP {health.status_code}"
                )
        except httpx.HTTPError as exc:
            problems += 1
            print(f"{FAIL} Cannot reach {settings.public_base_url}/health: {exc}")
            print("   Is uvicorn running and ngrok forwarding to port 8000?")

    print("\nReady for a test call." if not problems else f"\n{problems} problem(s) to fix.")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
