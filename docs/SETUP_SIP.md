# SIP setup: Twilio Elastic SIP Trunk → OpenAI GPT-Live → this app

How a call flows once this is set up:

```
caller ─► your mobile (forwards when you don't answer) ─► Twilio number
       ─► Twilio Elastic SIP Trunk (TLS signaling + SRTP media)
       ─► sip:<project id>@sip.api.openai.com  (GPT-Live answers the audio)
OpenAI ─► POST https://<public url>/openai/webhook   (live.transport.incoming)
app    ─► screen ─► accept (or reject) ─► sideband WebSocket for the whole call
```

Audio never passes through this app: it goes between Twilio and OpenAI only. The app decides
whether to answer, configures the agent, runs the functions (ask you on Telegram, save a
message, flag, hang up) and stores the transcript.

Do the steps in order. Each step names the `.env` variables it fills. Step 6 checks everything
at once.

| Step | You get | `.env` |
| --- | --- | --- |
| [1. OpenAI project](#1-openai-project-api-key-billing-project-id) | API key with billing, project ID | `OPENAI_API_KEY`, `OPENAI_PROJECT_ID` |
| [2. Public URL](#2-public-https-url) | OpenAI can reach your laptop | `PUBLIC_BASE_URL` |
| [3. OpenAI webhook](#3-openai-project-webhook) | Incoming calls reach the app, signed | `OPENAI_WEBHOOK_SECRET` |
| [4. Twilio number](#4-twilio-account-and-number) | A number that can take calls | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER` |
| [5. Elastic SIP Trunk](#5-twilio-elastic-sip-trunk) | The number's calls go to OpenAI | none |
| [6. Check](#6-check-everything) | One PASS/FAIL line per item | none |
| [7. Local test call](#7-local-test-call) | The first real call | none |

---

## 1. OpenAI project: API key, billing, project ID

1. Sign in at <https://platform.openai.com>. Top left, pick the **project** that will own the
   calls (or create one: project menu → **Create project**, e.g. "jarvis"). The API key, the
   webhook and the SIP URI must all belong to **this same project**.
2. **Billing**: Settings → Organization → **Billing** → add a payment method and some credit.
   GPT-Live is not available on the free tier.
3. **API key**: Settings → Project → **API keys** → **Create new secret key** → owned by you,
   project = the one above → copy it (it is shown once) into `OPENAI_API_KEY`.
4. **Project ID**: Settings → Project → **General** → *Project ID* (`proj_...`) → copy into
   `OPENAI_PROJECT_ID`.
5. Check the key and the model: `python scripts/check_sip.py --offline`. The first two lines
   must be ✅. Optionally run `python scripts/live_smoke.py --audio <recording>` to hear the
   agent without any telephony.

**EU data residency.** If the project was created with EU data residency, use
`sip-eu.api.openai.com` instead of `sip.api.openai.com` in step 5. Otherwise use the default.

## 2. Public HTTPS URL

OpenAI must reach `https://<your url>/openai/webhook`. For local tests this is your ngrok
static domain:

1. Sign up at <https://ngrok.com> (free plan) and install it: `brew install ngrok`.
2. Dashboard → **Your Authtoken** → copy it, then run `ngrok config add-authtoken <token>`.
3. Dashboard → **Domains** → claim your free static domain (`<name>.ngrok-free.dev`).
4. Start the tunnel to the app's port:

```bash
ngrok http --url=<your-domain>.ngrok-free.dev 8000
```

`PUBLIC_BASE_URL=https://<your-domain>.ngrok-free.dev` (no trailing slash). In production
it is the Fly.io / Render URL instead (README → Deployment).

## 3. OpenAI project webhook

1. platform.openai.com → Settings → Project → **Webhooks** (same project as step 1) →
   **Create**.
2. **Name**: `jarvis-incoming-calls`.
3. **URL**: `https://<your-domain>.ngrok-free.dev/openai/webhook`
4. **Event types**: tick **`live.transport.incoming`** only.
   - If `live.transport.incoming` is not in the list, GPT-Live SIP is not enabled for this
     project yet. Stop here and ask OpenAI support to enable GPT-Live SIP for the project
     (give them the project ID). Do **not** use `realtime.call.incoming`: that answers with
     the older Realtime API, and this app ignores it.
5. **Create** → the **signing secret** (`whsec_...`) is shown **once**. Copy it into
   `OPENAI_WEBHOOK_SECRET`. Lost it? Webhooks → the endpoint → **Rotate secret**, then update
   `.env` and restart the app.
6. Restart the app so it reads the new `.env` values.

What the app does with each delivery: it checks the signature (`webhook-id`,
`webhook-timestamp`, `webhook-signature`, max 5 minutes old) and answers **400** if it does
not match. Re-deliveries (same `webhook-id`) are ignored. Then it answers **200**
immediately and screens and accepts the call in the background. The webhook page's **Send
test event** button works too. A test `live.transport.incoming` carries a made-up session ID,
so the accept fails and you get a Telegram message "Gelen arama cevaplanamadı". For a test
event, that message is the expected result.

## 4. Twilio account and number

This step is unchanged from [SETUP.md §1](SETUP.md#1-twilio-number): an **upgraded (funded)**
account and a number with **Voice** capability. A Lithuanian number needs the regulatory
bundle; a US number does not but makes forwarding international. Fill `TWILIO_ACCOUNT_SID`,
`TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`.

On the SIP path the number does **not** use the "A call comes in" webhook: once it is attached
to the trunk (step 5), the trunk owns its incoming calls and Twilio shows its voice
configuration as "SIP Trunk".

## 5. Twilio Elastic SIP Trunk

Twilio Console (<https://console.twilio.com>). If **Elastic SIP Trunking** is not in the left
menu, open **Explore Products** and pin it. Twilio renames buttons from time to time; the
setting names below are the ones to look for.

1. **Elastic SIP Trunking → Manage → Trunks → Create new SIP Trunk**. Name: `jarvis-openai`.
2. **General** tab:
   - **Secure Trunking: Enabled.** Twilio then uses TLS for signaling and SRTP for audio.
     GPT-Live requires SRTP, so without this setting calls fail.
   - **Call Transfer (SIP REFER): Disabled.** The call always stays on one leg.
   - Leave **Disaster Recovery URL** empty for now.
   - **Save**.
3. **Termination** tab: leave empty. Termination is for calls *from* OpenAI out to the phone
   network, which this app never makes.
4. **Origination** tab → **Add new Origination URI**:
   - **Origination SIP URI**: `sip:proj_XXXXXXXXXXXXXXXX@sip.api.openai.com;transport=tls`
     (your `OPENAI_PROJECT_ID`; `python scripts/check_sip.py --offline` prints the exact line).
     `transport=tls` makes Twilio connect on port 5061 with TLS. Do not add a port, a path or
     credentials.
   - **Priority** `10`, **Weight** `10`, **Enabled** on → **Add**.
5. **Numbers** tab → **Add a number → Add an Existing Number** → tick your Twilio number →
   **Add selected**.

Nothing else is needed: OpenAI does not ask Twilio for credentials or IP allowlists. The
project ID in the URI tells OpenAI which project (and so which webhook) gets the call.

## 6. Check everything

With the app and ngrok running (step 7.1):

```bash
python scripts/check_sip.py
```

It checks the OpenAI key and model access, the project ID and secret, `/health` through your
public URL, a **signed** test webhook (must be accepted) and a **badly signed** one (must be
refused), and then the Twilio trunk: Secure Trunking on, the origination URI, the number
attached. Every line should be ✅ before the first call.

## 7. Local test call

### 7.1 Start

```bash
# terminal 1: the app
uvicorn app.main:app --reload
# terminal 2: the tunnel
ngrok http --url=<your-domain>.ngrok-free.dev 8000
# terminal 3
python scripts/check_sip.py
```

In Telegram, send `/status` to the bot. It must show `GPT-Live (SIP): hazır`. Set
`/mode` to "screen all" for the test.

### 7.2 Call

Call your **Twilio number directly** from your mobile (no forwarding yet: one thing at a time).
In the app log you should see, in order:

1. `Incoming SIP call accepted` (with `caller` and `reason=screening`)
2. `Sideband attached`
3. `AI + recording disclosure spoken`. A warning `disclosure not found ... sending it now`
   means the opening line was not spoken and the app made the agent say it. Note it in the
   checklist below.
4. `Delegation ...` and `Function call ask_owner` → a Telegram message with buttons. Answer it.
5. `Function call end_call` → `Hanging up: asistan bitirdi: ...` → `Session closed:
   close_requested`
6. `Call finished` → the Turkish report in Telegram.

### 7.3 Checklist

- [ ] The first sentence says it is an AI assistant and that the call is recorded/transcribed.
- [ ] Lithuanian is natural; replies are short; you can interrupt the agent.
- [ ] On hold the agent tells you to wait and does not go silent for long.
- [ ] Your Telegram answer is relayed correctly, in Lithuanian.
- [ ] The goodbye is heard **completely** before the line closes.
- [ ] Not answering on Telegram for 75 s → apology, message taken, polite end.
- [ ] Hanging up while on hold → Telegram shows "📵 Arayan beklerken kapattı".
- [ ] `/calls` lists the call with its summary.

### 7.4 Then forwarding

When the direct call works, set conditional call forwarding on your mobile to the Twilio
number ([SETUP.md §3](SETUP.md#3-gsm-conditional-call-forwarding)) and repeat from another
phone.

## Troubleshooting

| Symptom | Where to look | Likely cause |
| --- | --- | --- |
| Caller hears ringing then busy/failure, no webhook in the app log | Twilio Console → Monitor → **Call logs** → the call → SIP response; Twilio **Debugger** | `403/404`: wrong project ID in the origination URI, or the Request-URI reached OpenAI without the project ID (check the INVITE line: it must be `INVITE sip:proj_...@sip.api.openai.com`). `408`/timeout: GPT-Live SIP not enabled for the project, or TLS not used (`transport=tls` missing). `488`: Secure Trunking off (no SRTP). |
| Webhook arrives, app log `Rejected OpenAI webhook` | app log | `OPENAI_WEBHOOK_SECRET` differs from the endpoint's secret (rotate and update), or the machine clock is > 5 min off. |
| Webhook answered `503` | app log | `OPENAI_API_KEY` or `OPENAI_WEBHOOK_SECRET` missing in the running app's `.env`. |
| `Accept request failed` + Telegram "cevaplanamadı" | app log (the HTTP error) | API key from a **different project** than the webhook/SIP URI; or billing/model access. |
| Accepted, but silence and then the call drops | Twilio call log (media) | SRTP/codec negotiation: check Secure Trunking. |
| `Sideband dropped ... re-attaching` then `Sideband lost for good` | app log | Network from your laptop to OpenAI. The call is hung up and you get the transcript in Telegram. |
| `Goodbye wait capped; hanging up` | app log | The agent kept talking after `end_call`. Raise `LIVE_GOODBYE_MAX_SECONDS` if goodbyes get cut. |
| Call rejected right away, log `reason=...` | app log | Screening: denylist, `/mode off` with reject, `MAX_CONCURRENT_CALLS`, or the per-number rate limit. |

Settings that tune the call (all optional, see `.env.example`): `MAX_CALL_SECONDS`,
`WRAP_UP_SECONDS`, `MAX_OWNER_QUESTIONS`, `HOLD_TIMEOUT_SECONDS`,
`HOLD_PHRASE_INTERVAL_SECONDS`, `HOLD_STILL_TRYING_MAX`, `LIVE_DISCLOSURE_DEADLINE_SECONDS`,
`LIVE_GOODBYE_QUIET_SECONDS`, `LIVE_GOODBYE_MAX_SECONDS`, `LIVE_FALLBACK_GRACE_SECONDS`,
`LIVE_CONTROL_TIMEOUT_SECONDS`.
