"""Check the GPT-Live SIP setup before the first real call (docs/SETUP_SIP.md).

Usage:
    python scripts/check_sip.py            # everything (the app must be running and reachable)
    python scripts/check_sip.py --offline  # only .env values and the OpenAI key

Checks:
1. OPENAI_API_KEY works and the project can see LIVE_VOICE_MODEL.
2. OPENAI_PROJECT_ID looks like proj_...; prints the Twilio origination URI to paste.
3. OPENAI_WEBHOOK_SECRET is set (whsec_...).
4. PUBLIC_BASE_URL/health answers (tunnel or deployment is up).
5. A test webhook signed with OPENAI_WEBHOOK_SECRET is accepted at /openai/webhook, and one
   with a bad signature is refused: proves the URL routes to this app and the secrets match.
   (The test event type is ignored by the app; no call is started.)
6. Twilio: an Elastic SIP Trunk with Secure Trunking on, an origination URI pointing at
   sip:<project id>@sip.api.openai.com;transport=tls, and TWILIO_PHONE_NUMBER attached.
"""

import argparse
import base64
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings, get_settings  # noqa: E402
from app.rules import normalize_number  # noqa: E402

OK, FAIL, WARN = "✅", "❌", "⚠️ "
OPENAI_API = "https://api.openai.com/v1"
TRUNKING_API = "https://trunking.twilio.com/v1"
SIP_HOSTS = ("sip.api.openai.com", "sip-eu.api.openai.com")


def origination_uri(project_id: str) -> str:
    return f"sip:{project_id}@sip.api.openai.com;transport=tls"


def signed_headers(secret: str, body: str) -> dict[str, str]:
    msg_id, timestamp = f"msg_selftest_{int(time.time())}", str(int(time.time()))
    key = (
        base64.b64decode(secret.removeprefix("whsec_"))
        if secret.startswith("whsec_")
        else (secret.encode())
    )
    digest = hmac.new(key, f"{msg_id}.{timestamp}.{body}".encode(), hashlib.sha256).digest()
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": timestamp,
        "webhook-signature": "v1," + base64.b64encode(digest).decode(),
        "content-type": "application/json",
    }


def check_openai(settings: Settings) -> int:
    if settings.openai_api_key is None:
        print(f"{FAIL} OPENAI_API_KEY is not set in .env")
        return 1
    headers = {"Authorization": f"Bearer {settings.openai_api_key.get_secret_value()}"}
    try:
        response = httpx.get(
            f"{OPENAI_API}/models/{settings.live_voice_model}", headers=headers, timeout=15
        )
    except httpx.HTTPError as exc:
        print(f"{FAIL} OpenAI unreachable: {exc}")
        return 1
    if response.status_code == 401:
        print(f"{FAIL} OpenAI rejected OPENAI_API_KEY (401)")
        return 1
    if response.status_code != 200:
        print(
            f"{FAIL} The key works but {settings.live_voice_model} is not available to this "
            f"project (HTTP {response.status_code}). Check billing and the project's model access."
        )
        return 1
    print(f"{OK} OPENAI_API_KEY works and the project can use {settings.live_voice_model}")
    return 0


def check_env(settings: Settings) -> int:
    problems = 0
    project = settings.openai_project_id or ""
    if project.startswith("proj_"):
        print(f"{OK} OPENAI_PROJECT_ID set. Twilio origination URI:\n   {origination_uri(project)}")
    else:
        print(f"{FAIL} OPENAI_PROJECT_ID missing or not proj_... (Settings > Project > General)")
        problems += 1
    secret = settings.openai_webhook_secret
    if secret is None:
        print(f"{FAIL} OPENAI_WEBHOOK_SECRET is not set (shown once when creating the webhook)")
        problems += 1
    elif not secret.get_secret_value().startswith("whsec_"):
        print(f"{WARN} OPENAI_WEBHOOK_SECRET does not start with whsec_; is it the right value?")
    else:
        print(f"{OK} OPENAI_WEBHOOK_SECRET set")
    if not settings.public_base_url.startswith("https://"):
        print(
            f"{FAIL} PUBLIC_BASE_URL must be a public https:// URL (now {settings.public_base_url})"
        )
        problems += 1
    return problems


def check_webhook(settings: Settings) -> int:
    base = settings.public_base_url.rstrip("/")
    try:
        health = httpx.get(f"{base}/health", timeout=10)
    except httpx.HTTPError as exc:
        print(f"{FAIL} {base}/health unreachable ({exc}). Is the app running, is ngrok up?")
        return 1
    if health.status_code != 200:
        print(f"{FAIL} {base}/health answered HTTP {health.status_code}")
        return 1
    print(f"{OK} {base}/health answers")
    if settings.openai_webhook_secret is None:
        return 1

    url = f"{base}/openai/webhook"
    body = json.dumps({"id": "evt_selftest", "object": "event", "type": "dimidium.selftest"})
    headers = signed_headers(settings.openai_webhook_secret.get_secret_value(), body)
    good = httpx.post(url, content=body, headers=headers, timeout=10)
    bad = httpx.post(
        url, content=body, headers={**headers, "webhook-signature": "v1,AAAA"}, timeout=10
    )
    problems = 0
    if good.status_code == 200:
        print(f"{OK} Signed test webhook accepted at {url}")
    elif good.status_code == 503:
        print(f"{FAIL} {url} answered 503: the app has no OPENAI_API_KEY/OPENAI_WEBHOOK_SECRET")
        problems += 1
    else:
        print(
            f"{FAIL} Signed test webhook refused (HTTP {good.status_code}). The app runs with a "
            "different OPENAI_WEBHOOK_SECRET, or the server clock is off by > 5 minutes."
        )
        problems += 1
    if bad.status_code == 400:
        print(f"{OK} Webhook with a bad signature refused")
    else:
        print(f"{FAIL} Webhook with a bad signature answered HTTP {bad.status_code} (want 400)")
        problems += 1
    return problems


def check_twilio_trunk(settings: Settings) -> int:
    sid, token = settings.twilio_account_sid, settings.twilio_auth_token
    if not sid or token is None:
        print(f"{WARN} Twilio credentials not set; skipping the trunk check")
        return 0
    with httpx.Client(auth=(sid, token.get_secret_value()), timeout=15) as client:
        response = client.get(f"{TRUNKING_API}/Trunks")
        if response.status_code != 200:
            print(f"{FAIL} Twilio trunk list failed (HTTP {response.status_code})")
            return 1
        for trunk in response.json().get("trunks", []):
            urls = client.get(f"{TRUNKING_API}/Trunks/{trunk['sid']}/OriginationUrls").json()
            openai_urls = [
                u
                for u in urls.get("origination_urls", [])
                if any(h in u["sip_url"] for h in SIP_HOSTS)
            ]
            if openai_urls:
                return _check_trunk(client, settings, trunk, openai_urls)
    print(f"{FAIL} No Elastic SIP Trunk has an origination URI on sip.api.openai.com")
    return 1


def _check_trunk(
    client: httpx.Client, settings: Settings, trunk: dict, urls: list[dict]  # type: ignore[type-arg]
) -> int:
    problems = 0
    print(f"{OK} Trunk '{trunk.get('friendly_name')}' routes to OpenAI")
    if trunk.get("secure"):
        print(f"{OK} Secure Trunking is on (TLS + SRTP)")
    else:
        print(f"{FAIL} Secure Trunking is off: GPT-Live requires SRTP media")
        problems += 1
    for url in urls:
        sip_url = url["sip_url"]
        ok = (
            url.get("enabled")
            and "transport=tls" in sip_url
            and (not settings.openai_project_id or settings.openai_project_id in sip_url)
        )
        mark = OK if ok else FAIL
        print(f"{mark} Origination URI {sip_url} (enabled={url.get('enabled')})")
        if not ok:
            print(f"   want: {origination_uri(settings.openai_project_id or 'proj_...')}")
            problems += 1
    numbers = client.get(f"{TRUNKING_API}/Trunks/{trunk['sid']}/PhoneNumbers").json()
    attached = {normalize_number(n["phone_number"]) for n in numbers.get("phone_numbers", [])}
    own = settings.twilio_phone_number
    if own and normalize_number(own) in attached:
        print(f"{OK} {own} is attached to the trunk")
    else:
        print(f"{FAIL} TWILIO_PHONE_NUMBER ({own or 'not set'}) is not attached to this trunk")
        problems += 1
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--offline", action="store_true", help="skip the network checks")
    args = parser.parse_args()
    settings = get_settings()
    problems = check_env(settings) + check_openai(settings)
    if not args.offline:
        problems += check_webhook(settings) + check_twilio_trunk(settings)
    print()
    print("All SIP checks passed." if not problems else f"{problems} problem(s) found.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
