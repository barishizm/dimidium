# Setup guide

From "nothing set up" to calls on your mobile being answered by Jarvis. Work through the
sections in order. Deployment (Fly.io or Render) is in the README under
[Deployment](../README.md#deployment). For a first test on your laptop with ngrok, see
[live_call_test.md](live_call_test.md).

| Step | You get | `.env` variables |
| --- | --- | --- |
| [1. Twilio number](#1-twilio-number) | A phone number that can run the agent | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER` |
| [2. Voice webhook](#2-voice-webhook) | Calls to that number reach the app | `PUBLIC_BASE_URL` |
| [3. Call forwarding](#3-gsm-conditional-call-forwarding) | Calls you miss go to that number | none (set on your phone) |
| [4. Telegram bot](#4-telegram-bot) | The agent can ask you, and you can answer | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_CHAT_ID` |
| [5. Costs](#5-cost-per-call-minute) | What each minute costs | none |

When everything is filled in, run the doctor. It prints one PASS/FAIL row per check:

```bash
python scripts/doctor.py                # locally
fly ssh console -C "python scripts/doctor.py"   # on Fly.io (Render: Shell tab)
```

---

## 1. Twilio number

### 1.1 Account

1. Sign up at <https://www.twilio.com/try-twilio> and verify your email and mobile number.
2. **Upgrade** the account (Console, top bar → **Upgrade**, or Admin → Account billing) and
   add funds with a card. On a trial account, callers first hear a trial notice and only
   numbers you verified can call in. A trial account cannot carry real forwarded calls.
3. Console home → **Account Info**: copy **Account SID** into `TWILIO_ACCOUNT_SID` and
   **Auth Token** (click *Show*) into `TWILIO_AUTH_TOKEN`. The Auth Token also signs every
   webhook. If it leaks, rotate it in Console → Account → API keys & tokens and update
   `.env` and your deployment secrets.

### 1.2 Buying a number

Console → **Phone Numbers → Manage → Buy a number** → choose the country, tick **Voice**, then
**Search** → **Buy**. Put the number into `TWILIO_PHONE_NUMBER` in E.164 form
(`+370...`, `+1...`).

**Lithuanian numbers need a regulatory bundle.** Telecom rules in Lithuania (as in most EU
countries) require Twilio to know who uses a local or mobile number. Before buying, Twilio
asks for a *Regulatory Bundle*: Console → **Phone Numbers → Regulatory Compliance →
Bundles → Create a Bundle**, end user type *Individual*, then your ID document and proof of
address. The Console shows the exact documents for the number type you picked. Review takes
from hours to several business days; the number can only be bought once the bundle is
approved. The address and ID stay at Twilio; nothing of it is used by this app.

**Alternatives** if you do not want to wait for (or cannot provide) a bundle:

| Option | Pros | Cons |
| --- | --- | --- |
| A **US local number** (no bundle) | Instant, about $1.15/month, cheap inbound minutes | Your Lithuanian operator charges every forwarded call as an **international call** (see 3.4). Usually the most expensive option per minute. |
| Another **EU country's number** (Estonia, Latvia, ...) | Still in the EU | Most also need a bundle; forwarding to it is international too |
| **Another provider** (Telnyx, Vonage, Zadarma, a local SIP operator) with Lithuanian numbers | Sometimes simpler paperwork, EU media | The app is Twilio-specific (TwiML, Media Streams, signature check). Pipecat has serializers for Telnyx and Plivo, but switching needs code changes. |
| **No number yet** | Free | `scripts/fake_twilio_call.py` tests the full pipeline without Twilio ([live_call_test.md](live_call_test.md) step 0) |

Recommendation: a Lithuanian number with a bundle. Forwarding to it is a domestic call
(usually included in your plan), and callers never see a foreign number.

### 1.3 Check

```bash
python scripts/check_twilio.py   # credentials, number on the account, webhook, tunnel
```

---

## 2. Voice webhook

Twilio asks the app what to do with each call by POSTing to `https://<domain>/twilio/voice`.
`<domain>` is your deployment (`<app>.fly.dev`, `<service>.onrender.com`, your own domain,
or the ngrok domain during local tests).

1. Set `PUBLIC_BASE_URL=https://<domain>` in the app's environment, with no trailing slash
   and no path. It must be **exactly** the URL Twilio calls: the app checks Twilio's
   signature against it, and a mismatch rejects every call with 403.
2. Console → **Phone Numbers → Manage → Active numbers** → click your number → **Configure**.
3. Under **Voice Configuration**:
   - *Configure with*: **Webhook, TwiML Bin, Function, Studio Flow, Proxy Service**
   - *A call comes in*: **Webhook**, URL `https://<domain>/twilio/voice`, method **HTTP POST**
   - *Primary handler fails* (optional, recommended): a TwiML Bin with
     `<Response><Say language="lt-LT">Atsiprašome, šiuo metu negalime atsiliepti.</Say></Response>`,
     so callers hear something if the app is down (have the text checked by a native speaker).
   - Leave *Call status changes* empty.
4. **Save configuration**.

Or let the script set it from `PUBLIC_BASE_URL`:

```bash
python scripts/check_twilio.py --set-webhook
```

Media goes over a WebSocket to `wss://<domain>/twilio/stream`. The app builds that URL from
`PUBLIC_BASE_URL`; there is nothing to configure in Twilio for it.

Security, all on by default:

- Requests without a valid `X-Twilio-Signature` are rejected (`TWILIO_VALIDATE_SIGNATURE=true`;
  never turn it off on a public server).
- The media stream only accepts the one-time token from the app's own TwiML.
- Rate limits on `/twilio/voice`: `VOICE_REQUESTS_PER_IP_PER_MINUTE` per IP (HTTP 429), at most
  `CALLS_PER_CALLER` calls per number within `CALLS_PER_CALLER_WINDOW_SECONDS` (busy signal;
  numbers in `do_not_answer.allow` are exempt), and `MAX_CONCURRENT_CALLS` at once (busy).
  Callers with a hidden number share one limit.

---

## 3. GSM conditional call forwarding

Your mobile stays your number. Calls you do not take are forwarded by your operator to the
Twilio number, and Jarvis answers them. Use **conditional** forwarding only: unconditional
forwarding (`**21*`, and the iPhone *Settings → Phone → Call Forwarding* switch) sends every
call to the agent, and your phone never rings.

### 3.1 Codes

Type the code in the phone app and press call. Use the full international form of the Twilio
number, e.g. `+37052000000`.

| What | Code |
| --- | --- |
| Forward when **unanswered** | `**61*<number>#` |
| Forward when **busy** (or you press decline) | `**67*<number>#` |
| Forward when **unreachable** (phone off, no signal) | `**62*<number>#` |
| Check the current setting | `*#61#`, `*#67#`, `*#62#` |
| **Cancel all conditional forwarding** | `##004#` |

These are the standard GSM (MMI) codes. **Operators differ:** some (Telia, Tele2 and Bitė in
Lithuania included) also offer the setting in their self-service app, some do not support
every code, and some reset forwarding when your plan changes. **Verify with your carrier**,
then test (3.3).

Cancelling with `##004#` also removes forwarding to your operator voicemail. To get your
voicemail back, ask your operator for its voicemail number or code.

### 3.2 Ring time before forwarding

The unanswered forward waits for a carrier-set ring timeout, typically around 20-30 seconds.
Many carriers let you set it, in steps of 5 seconds from 5 to 30:

```
**61*<number>**<seconds>#        e.g. **61*+37052000000**20#
```

Shorter means callers wait less, but you have less time to pick up yourself.

### 3.3 Test it

1. From another phone, call your mobile and do not answer: Jarvis should answer after the
   ring timeout, and you get the Telegram messages.
2. Call again and decline: busy forwarding.
3. Switch on flight mode and call: unreachable forwarding.

Caller ID: Twilio normally receives the original caller's number, so the rules in
`rules.yaml` work. If every call shows your own mobile number instead, your operator replaces
it on forwarding; ask them about it.

### 3.4 Charges

**The forwarded leg is billed to you by your operator**, as a call from your phone to the
Twilio number:

- Twilio number in Lithuania: a domestic call, usually included in your plan.
- Twilio number abroad (e.g. a US number): an **international call per forwarded minute**,
  often far more expensive than everything else in the cost table below. Check your plan
  first.
- While you are **roaming**, forwarding can be charged as roaming calls even when you never
  touch the phone.

---

## 4. Telegram bot

The agent asks you through your own bot. Only your chat (`TELEGRAM_OWNER_CHAT_ID`) is
treated as the owner; messages from anyone else are ignored.

1. In Telegram, open **@BotFather** (check the blue verified tick) and send `/newbot`.
2. Send a display name (e.g. `Jarvis`), then a username ending in `bot`
   (e.g. `jarvis_barish_bot`).
3. BotFather replies with a token like `123456789:AA...`. Put it into `TELEGRAM_BOT_TOKEN`.
   Treat it like a password; `/revoke` in BotFather replaces it.
4. Recommended: `/setjoingroups` → your bot → **Disable**, so nobody can add it to a group.
5. Open your bot (`https://t.me/<username>`) and press **Start**.
6. Find your chat ID:

   ```bash
   python scripts/check_telegram.py   # prints the chat ID of your /start message
   ```

   Put it into `TELEGRAM_OWNER_CHAT_ID`, then run the script again: it sends a test message.

Only one process may receive the bot's updates. Stop the local app (or the simulator) when
the deployed app is running, or you will see `Conflict: terminated by other getUpdates`
errors in the logs.

---

## 5. Cost per call minute

Everything is billed by the minute or by usage, so the cost of one call minute is:

```
cost/min = T_in + T_stream + STT + TTS_chars × P_tts + LLM
LLM      = turns/min × (input_tokens × P_in + output_tokens × P_out)
```

| Term | What | Example price (check the linked page) |
| --- | --- | --- |
| `T_in` | Twilio inbound minute on your number | US local $0.0085/min; Lithuanian numbers are listed lower (about $0.004/min). [Twilio LT pricing](https://www.twilio.com/en-us/voice/pricing/lt), [US](https://www.twilio.com/en-us/voice/pricing/us) |
| `T_stream` | Twilio Media Streams (the audio WebSocket) | $0.0044/min ([pricing](https://www.twilio.com/en-us/voice/pricing/us)) |
| `STT` | Speech-to-text runs for the **whole** call, hold included (what the caller says on hold is transcribed) | Google STT v2 Chirp ~$0.016/min, Azure real-time ~$0.017/min, Deepgram Nova-3 below $0.01/min |
| `TTS_chars × P_tts` | Only what the agent says live. Greeting, fillers and hold phrases are synthesized once and cached. About 300-400 characters per call minute. | Google Chirp 3 HD $30 per 1M chars (≈ $0.011/min), Azure Neural $15 per 1M (≈ $0.005/min) |
| `LLM` | About 4 turns per minute. Each turn sends ~2k tokens of system prompt and tools (cached) plus the conversation so far (~1-3k), and gets ~50-150 tokens back | Claude Haiku 5.5: $0.10 per 1M input, $0.50 per 1M output → ≈ $0.002/min. The post-call summary adds about $0.001 per call |

Worked example: a Lithuanian Twilio number with Google STT and TTS and Claude Haiku 5.5:

```
0.004 + 0.0044 + 0.016 + 0.011 + 0.002 ≈ $0.037 per minute  →  a 2-minute call ≈ $0.08
```

Speech recognition and synthesis are about 70% of it. The LLM is under 10%.

**Not in the per-minute formula:**

- Your operator's charge for the forwarded leg (section 3.4). It is zero with a domestic number
  and an unlimited plan, but it can be the biggest item with a foreign number.
- Monthly fixed costs: the Twilio number (US $1.15/month; Lithuanian prices are on the Twilio
  LT page), hosting (Fly.io `shared-cpu-1x` 1 GB about $6/month plus about $0.15 for a 1 GB
  volume; Render Starter $7/month plus disk), and the regulatory bundle (free).
- Telegram is free.

Prices change. Check the pricing pages before you rely on the numbers. Twilio's usage page
(Console → Monitor → Usage) shows what real calls cost.
