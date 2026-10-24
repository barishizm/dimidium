# SIP setup: Telnyx → OpenAI GPT-Live → this app

How a call flows once this is set up:

```
caller ─► your mobile (forwards when you don't answer) ─► Telnyx number (+370...)
       ─► Telnyx FQDN connection (TLS signaling + SRTP media)
       ─► sip:<project id>@sip.api.openai.com:5061;transport=tls  (GPT-Live answers the audio)
OpenAI ─► POST https://<public url>/openai/webhook   (live.transport.incoming)
app    ─► screen ─► accept (or reject) ─► sideband WebSocket for the whole call
```

Audio never passes through this app: it goes between Telnyx and OpenAI only. The app decides
whether to answer, configures the agent, runs the functions (ask you on Telegram, save a
message, flag, hang up) and stores the transcript.

Sources: [Telnyx GPT-Live configuration guide](https://developers.telnyx.com/docs/voice/sip-trunking/gpt-live-configuration-guide)
and [OpenAI Telephony and SIP](https://developers.openai.com/api/docs/guides/voice-sip). Telnyx
and OpenAI rename buttons from time to time; the setting names below are the ones to look for
(the portal's search box helps).

Do the steps in order. Each step names the `.env` variables it fills. Step 8 checks everything
at once.

| Step | You get | `.env` |
| --- | --- | --- |
| [1. Telnyx account](#1-telnyx-account) | A funded, verified account | none |
| [2. Lithuanian number](#2-buy-the-lithuanian-number) | A number that can take calls | `TELNYX_NUMBER` |
| [3. FQDN connection](#3-create-the-sip-connection-fqdn) | A route from Telnyx to OpenAI | none |
| [4. Assign the number](#4-assign-the-number-and-set-translated-number) | The number's calls use that route and carry your project ID | none |
| [5. OpenAI project](#5-openai-project-billing-api-key-project-id) | API key with billing, project ID | `OPENAI_API_KEY`, `OPENAI_PROJECT_ID` |
| [6. Public URL](#6-public-https-url) | OpenAI can reach your laptop | `PUBLIC_BASE_URL` |
| [7. OpenAI webhook](#7-openai-project-webhook) | Incoming calls reach the app, signed | `OPENAI_WEBHOOK_SECRET` |
| [8. Check](#8-check-everything) | One PASS/FAIL line per item | `TELNYX_API_KEY` (optional) |
| [9. Test call](#9-local-test-call) | The first real call | none |
| [10. Forwarding](#10-then-forwarding) | Missed calls reach the agent | none |

---

## 1. Telnyx account

1. Sign up at <https://telnyx.com/sign-up> and verify your email and mobile number.
2. Open **Mission Control** (<https://portal.telnyx.com>). Complete the account verification
   Telnyx asks for (identity details; Telnyx shows what it needs when you first try to buy a
   number or add funds).
3. **Billing → Payment** (the portal's billing section): add a card and top up a small balance.
   Numbers and per-minute charges are drawn from it.

## 2. Buy the Lithuanian number

1. Mission Control → **Numbers → Buy Numbers**.
2. **Country**: Lithuania. **Features**: tick **Voice only**; leave SMS/MMS **unticked**.
   Telnyx's Lithuanian stock is voice-only (checked through its API on 2026-10-24: local
   numbers with area codes 37, 31, 46 and 34, and mobile numbers starting 66; none with SMS),
   so a search that also requires SMS finds nothing and the portal says it has "no search
   coverage" for the country. Number type: local or mobile. Press **Search**.
   If a voice-only search still finds nothing, the account is probably not upgraded yet
   (Telnyx refuses some requests with "not permitted at this account level, refer to
   telnyx.com/upgrade"): finish identity verification and add funds first (step 1).
3. Pick a number and add it to the cart, then check out.
4. **Document verification.** Lithuanian numbers need regulatory documents. Telnyx shows the exact
   list and upload form for the number type you picked (typically proof of identity and
   address; the Telnyx docs do not list Lithuania's requirements, so follow the form). Plan on
   **about 72 hours** until the numbers are approved and active. Until then you can do steps 3, 5, 6 and 7.
   The documents stay at Telnyx; this app never sees them.
5. When the number shows as **active** under **Numbers → My Numbers**, copy it in E.164 form
   (`+370...`) into `TELNYX_NUMBER`.

If you cannot wait, `python scripts/live_smoke.py` runs the agent over a WebSocket with a
recorded caller, no number needed ([GPT_LIVE_DESIGN.md](GPT_LIVE_DESIGN.md)).

## 3. Create the SIP connection (FQDN)

1. Mission Control → **Voice → SIP Trunking** → **Add SIP Connection**.
2. **Name**: `OpenAI GPT-Live`. **Type**: **FQDN**. Save/create.
3. **Authentication and routing → Add FQDN**:
   - **DNS record type**: `A`
   - **FQDN**: `sip.api.openai.com`
   - **Port**: `5061`

   Save. OpenAI needs no username, password or IP allowlist: the project ID (step 4) tells it
   which project gets the call.
4. **Inbound** settings of the same connection:
   - **SIP transport protocol** (inbound transport): **TLS**. TLS secures the signaling only.
   - **Encrypted media**: **SRTP Mandatory**. This is what encrypts the audio; without it the
     call fails or has no sound.
   - **Codecs**: tick **Opus**, **PCMU** (shown as G711U) and **PCMA** (G711A) and put them in
     that order. Untick the others (G722, G729, ...). Opus is wideband; PCMU/PCMA are the
     narrowband fallback. (Telnyx offers Opus only over TCP/TLS, which this connection uses.)
   - Do **not** set up Media Streaming for this connection: the audio goes straight to OpenAI.
5. Make sure the connection is **Active** (on).

> EU data residency: if your OpenAI project uses it, OpenAI documents a different SIP host for
> it. Check OpenAI's SIP guide; `scripts/doctor.py` accepts `sip-eu.api.openai.com` as well.
> (Not verified here.)

## 4. Assign the number and set Translated Number

1. Mission Control → **Numbers → My Numbers** → your Lithuanian number.
2. **Connection / Voice**: assign the **OpenAI GPT-Live** connection from step 3.
3. In the number's **Voice** settings, set **Translated Number** to your **OpenAI project ID**,
   including the `proj_` prefix (get it in step 5). Save.

Telnyx now rewrites every call to this number to
`sip:proj_...@sip.api.openai.com:5061;transport=tls`. That destination is what routes the call
to your OpenAI project.

## 5. OpenAI project: billing, API key, project ID

1. Sign in at <https://platform.openai.com>. Top left, pick the **project** that will own the
   calls (or create one: project menu → **Create project**, e.g. "jarvis"). The API key, the
   webhook and the Translated Number must all belong to **this same project**, otherwise
   OpenAI answers `session_id_not_found`.
2. **Billing**: Settings → Organization → **Billing** → add a payment method and some credit.
   GPT-Live is not available on the free tier.
3. **API key**: Settings → Project → **API keys** → **Create new secret key** → project = the
   one above → copy it (shown once) into `OPENAI_API_KEY`.
4. **Project ID**: Settings → Project → **General** → *Project ID* (`proj_...`) → copy into
   `OPENAI_PROJECT_ID`, and into the Telnyx **Translated Number** (step 4).
5. **GPT-Live SIP must be enabled for the project.** OpenAI's SIP guide says to confirm this and
   does not say how; no API reports it. Signs it is **not** enabled: `live.transport.incoming`
   is missing from the webhook event list (step 7), or accept returns HTTP 403. If so, ask
   OpenAI support to enable GPT-Live SIP for the project and give them the project ID.

## 6. Public HTTPS URL

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
it is the Fly.io / Render URL instead (README → Deployment). The tunnel carries webhooks only,
never call audio.

## 7. OpenAI project webhook

1. platform.openai.com → Settings → Project → **Webhooks** (same project as step 5) →
   **Create**.
2. **Name**: `jarvis-incoming-calls`.
3. **URL**: `https://<your-domain>.ngrok-free.dev/openai/webhook`
4. **Event types**: tick **`live.transport.incoming`** only.
   - Do **not** tick the deprecated `live.call.incoming` or the Realtime
     `realtime.call.incoming`: this app ignores both (one handler decides; the first accept or
     reject wins, a later one returns `decision_already_made`).
   - If `live.transport.incoming` is not in the list, see step 5.5.
5. **Create** → the **signing secret** (`whsec_...`) is shown **once**. Copy it into
   `OPENAI_WEBHOOK_SECRET`. Lost it? Webhooks → the endpoint → **Rotate secret**, then update
   `.env` and restart the app.
6. Restart the app so it reads the new `.env` values.

What the app does with each delivery: it checks the signature (`webhook-id`,
`webhook-timestamp`, `webhook-signature`, max 5 minutes old) and answers **400** if it does
not match. Re-deliveries (same `webhook-id`) are ignored. Then it answers **200**
immediately and screens and accepts the call in the background, using `data.session_id`
(never the SIP Call-ID) for accept, reject and hang-up. The webhook page's **Send test event**
button works too: a test `live.transport.incoming` carries a made-up session ID, so the accept
fails and you get a Telegram message "Gelen arama cevaplanamadı" with the reason. For a test
event, that message is the expected result.

## 8. Check everything

Optional but useful: a Telnyx API key lets the doctor audit the Telnyx side too (read-only).
Mission Control → **API Keys** (search for it) → **Create API Key** → copy into
`TELNYX_API_KEY`. The running app never uses it.

With the app and ngrok running (step 9.1):

```bash
python scripts/doctor.py
```

It prints a PASS/FAIL table:

| Group | What it proves |
| --- | --- |
| `env ...` | Variables set; `OPENAI_PROJECT_ID` is `proj_...`; `TELNYX_NUMBER` is E.164; `PUBLIC_BASE_URL` is https |
| `OpenAI ...`, `OpenAI key project` | The key works, both models are available, the key belongs to `OPENAI_PROJECT_ID` (when OpenAI reports the project) |
| `GPT-Live call control` | The key can reach GPT-Live call control (a reject for a made-up session). A 403 here means GPT-Live SIP is probably not enabled |
| `Signature ...`, `Webhook ...` | A locally signed test payload verifies; your public URL accepts a signed one and refuses a badly signed one |
| `Telnyx ...` (with `TELNYX_API_KEY`) | The number is active, its connection is FQDN `sip.api.openai.com:5061` with TLS, SRTP and usable codecs, Translated Number equals the project ID, and call recording is off |

Every row should be PASS before the first call. `python scripts/doctor.py --offline` checks
only the `.env` values and the local signature (no network). What the doctor cannot prove:
that GPT-Live SIP is enabled for the project and that audio flows both ways. Only the test
call shows that.

## 9. Local test call

### 9.1 Start

```bash
# terminal 1: the app
uvicorn app.main:app --reload
# terminal 2: the tunnel
ngrok http --url=<your-domain>.ngrok-free.dev 8000
# terminal 3
python scripts/doctor.py
```

In Telegram, send `/status` to the bot. It must show `GPT-Live (SIP): hazır`. Set
`/mode` to "screen all" for the test.

### 9.2 Call

Call your **Telnyx number directly** from your mobile (no forwarding yet: one thing at a time).
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

### 9.3 Checklist

- [ ] The first sentence says it is an AI assistant and that the call is recorded/transcribed.
- [ ] Lithuanian is natural; replies are short; you can interrupt the agent.
- [ ] You hear the agent **and** it hears you (two-way audio).
- [ ] On hold the agent tells you to wait and does not go silent for long.
- [ ] Your Telegram answer is relayed correctly, in Lithuanian.
- [ ] The goodbye is heard **completely** before the line closes.
- [ ] Not answering on Telegram for 75 s → apology, message taken, polite end.
- [ ] Hanging up while on hold → Telegram shows "📵 Arayan beklerken kapattı".
- [ ] `/calls` lists the call with its summary.

## 10. Then forwarding

When the direct call works, set conditional call forwarding on your mobile to the Telnyx
number ([SETUP.md §3](SETUP.md#3-gsm-conditional-call-forwarding)) and repeat from another
phone.

---

## Troubleshooting

| Symptom | Where to look | Likely cause and fix |
| --- | --- | --- |
| **No webhook arrives** (caller hears ringing/busy, nothing in the app log) | Telnyx SIP diagnostics (below); OpenAI Webhooks page (delivery log); `python scripts/doctor.py` | In order: (1) the INVITE never left Telnyx: the number is not assigned to the connection, or Translated Number is empty/wrong; (2) the INVITE reached OpenAI without `proj_...` in the Request-URI; (3) the webhook URL is wrong or the tunnel is down (`Webhook ...` rows); (4) the webhook is not subscribed to `live.transport.incoming`; (5) GPT-Live SIP is not enabled for the project (step 5.5). |
| **HTTP 403 on accept** (log `Accept request failed`, Telegram names the cause) | app log, `accept_failure=sip_not_enabled` | GPT-Live SIP is not enabled for the project, or the key lacks access. Ask OpenAI support; check `GPT-Live call control` in the doctor. |
| **404 `session_id_not_found` on accept** | app log, `accept_failure=session_not_found` | API key, webhook and Translated Number are not in the **same project** (doctor: `OpenAI key project`, `Telnyx Translated Number`); or the caller already hung up; or a Send-test-event with a made-up ID. |
| **`decision_already_made`** | app log | Another handler already accepted or rejected the same call: a second webhook endpoint (e.g. one subscribed to `realtime.call.incoming`) in the same project. Keep one. |
| **Caller hears busy (486) or "unavailable" (480)** | app log `Incoming SIP call rejected (486)` with `reason=...` | Our screening declined it on purpose: denylist, `/mode off` with reject, `MAX_CONCURRENT_CALLS`, the per-number rate limit, a call from `TELNYX_NUMBER` itself (forwarding loop), or the server was shutting down (480). If there is **no** such log line, Telnyx or OpenAI declined: read the SIP response in the Telnyx diagnostics. |
| **One-way audio or silence** (you hear nothing, or the agent never reacts) | Telnyx diagnostics: SDP answer, selected codec, SRTP, RTP both ways | Encrypted media is not **SRTP Mandatory** (TLS does not encrypt audio); no common codec (see below); a firewall between you and Telnyx drops RTP (only for a SIP phone, not the PSTN path). The ngrok tunnel never carries audio, so it cannot be the cause. |
| **Call drops after N seconds** | Telnyx diagnostics: who sent BYE and when; app log `Hanging up: ...` | App-side limits first: `MAX_CALL_SECONDS` (default 300) with a wrap-up at `WRAP_UP_SECONDS` before it; `Hanging up: asistan bitirdi` (the agent called `end_call`); `Sideband lost for good` (the app's connection to OpenAI broke; fallback then hang-up). If the app log shows nothing, the drop is at SIP level: typically a very short drop (~30 s) is a missing ACK or a signaling path problem; a drop at a fixed interval is a SIP session timer or no media (RTP) for a while. Capture the call flow and look at who sent the BYE. |
| **Codec negotiation failure** (SIP 488 "Not Acceptable Here", or call connects with no audio) | Telnyx diagnostics: SDP offer and answer | The connection offers no codec OpenAI accepts. Enable **Opus, PCMU, PCMA** (step 3.4). Opus only works over TLS/TCP, which this connection uses; untick G722/G729. The doctor's `Telnyx codecs` row checks this. |
| Webhook arrives, app log `Rejected OpenAI webhook` | app log | `OPENAI_WEBHOOK_SECRET` differs from the endpoint's secret (rotate and update), or the machine clock is > 5 min off. |
| Webhook answered `503` | app log | `OPENAI_API_KEY` or `OPENAI_WEBHOOK_SECRET` missing in the running app's `.env`. |
| `Sideband dropped ... re-attaching` then `Sideband lost for good` | app log | Network from your laptop to OpenAI. The call is hung up and you get the transcript in Telegram. |
| `Goodbye wait capped; hanging up` | app log | The agent kept talking after `end_call`. Raise `LIVE_GOODBYE_MAX_SECONDS` if goodbyes get cut. |

### What to check in Telnyx SIP diagnostics

Telnyx's guide says to use its SIP call diagnostics and packet captures. Look in Mission
Control for the call detail / SIP debugging tools (search the portal for "SIP" or "Debugging";
I could not confirm the exact menu names from the docs). For a failing call, read in this order:

1. **Request-URI** of the INVITE sent to OpenAI: must be `sip:proj_...@sip.api.openai.com:5061`
   with `transport=tls`. Missing or wrong project ID → Translated Number (step 4.3).
2. **SIP response codes** from OpenAI: `200` answered; `403` not allowed (project/enablement);
   `404` unknown destination (project ID typo); `408`/timeout (TLS or port problem);
   `486`/`603` declined (our screening, or OpenAI's decision); `488` media not acceptable
   (codec/SRTP).
3. **SDP offer and answer**: the selected **codec**, and **SRTP** (`RTP/SAVP` with `a=crypto`
   lines). A plain `RTP/AVP` means encrypted media is off.
4. **Who sent the BYE**, and when (for drops).
5. **Packet loss and jitter** (for choppy audio).

Redact credentials and inline SRTP key material (`a=crypto`) before sharing a trace. Keep these
IDs apart in your notes: the OpenAI session ID (`data.session_id`), the webhook ID, the Telnyx
call identifiers and the SIP Call-ID. Only `data.session_id` works with OpenAI's accept,
reject and hang-up.

Settings that tune the call (all optional, see `.env.example`): `MAX_CALL_SECONDS`,
`WRAP_UP_SECONDS`, `MAX_OWNER_QUESTIONS`, `HOLD_TIMEOUT_SECONDS`,
`HOLD_PHRASE_INTERVAL_SECONDS`, `HOLD_STILL_TRYING_MAX`, `LIVE_DISCLOSURE_DEADLINE_SECONDS`,
`LIVE_GOODBYE_QUIET_SECONDS`, `LIVE_GOODBYE_MAX_SECONDS`, `LIVE_FALLBACK_GRACE_SECONDS`,
`LIVE_CONTROL_TIMEOUT_SECONDS`.
