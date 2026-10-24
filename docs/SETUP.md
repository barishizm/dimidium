# Setup guide

From "nothing set up" to calls on your mobile being answered by Jarvis. Work through the
sections in order. Deployment (Fly.io or Render) is in the README under
[Deployment](../README.md#deployment). The Telnyx number, the SIP connection, the OpenAI
webhook and a first test call on your laptop with ngrok are in
[SETUP_TELNYX.md](SETUP_TELNYX.md).

| Step | You get | `.env` variables |
| --- | --- | --- |
| [1. Telnyx number](#1-telnyx-number) | A phone number that can run the agent | `TELNYX_NUMBER` |
| [2. SIP connection and webhook](#2-sip-connection-and-openai-webhook) | Calls to that number reach GPT-Live and the app | `OPENAI_*`, `PUBLIC_BASE_URL` |
| [3. Call forwarding](#3-gsm-conditional-call-forwarding) | Calls you miss go to that number | none (set on your phone) |
| [4. Telegram bot](#4-telegram-bot) | The agent can ask you, and you can answer | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_CHAT_ID` |
| [5. Costs](#5-cost-per-call-minute) | What each minute costs | none |

When everything is filled in, run the doctor. It prints one PASS/FAIL row per check:

```bash
python scripts/doctor.py                # locally
fly ssh console -C "python scripts/doctor.py"   # on Fly.io (Render: Shell tab)
```

---

## 1. Telnyx number

Sign up at <https://telnyx.com/sign-up>, fund the account, and buy a **Lithuanian number with
Voice** (Mission Control → Numbers → Buy Numbers). Lithuanian numbers need document
verification, about 72 hours. The click-by-click steps are in
[SETUP_TELNYX.md §1-2](SETUP_TELNYX.md#1-telnyx-account). Put the number in E.164 form
(`+370...`) into `TELNYX_NUMBER`.

Why a Lithuanian number: forwarding to it is a domestic call (usually included in your plan),
and callers never see a foreign number.

**Alternatives** if you do not want to wait for the verification:

| Option | Pros | Cons |
| --- | --- | --- |
| A **number in another country** at Telnyx | Often instant | Your Lithuanian operator charges every forwarded call as an **international call** (see 3.4). Usually the most expensive option per minute. |
| **Another SIP provider** with Lithuanian numbers | Sometimes simpler paperwork | Untested here. Any SIP trunk that can send calls to `sip:<project>@sip.api.openai.com:5061` over TLS with SRTP should work; the app itself never talks to the carrier during a call. |
| **No number yet** | Free | `scripts/live_smoke.py` runs the agent over a WebSocket with a recorded caller ([SETUP_TELNYX.md](SETUP_TELNYX.md)) |

Check: `python scripts/doctor.py` (with the optional `TELNYX_API_KEY` it looks the number up).

---

## 2. SIP connection and OpenAI webhook

Calls go from your Telnyx number over an FQDN SIP connection (TLS + SRTP) straight to OpenAI
GPT-Live. OpenAI then tells the app about the call with a signed webhook to
`https://<domain>/openai/webhook`, and the app accepts it and follows it over a sideband
WebSocket. Audio never passes through the app. The step-by-step setup is in
[SETUP_TELNYX.md](SETUP_TELNYX.md).

Security, all on by default:

- Webhooks without a valid OpenAI signature (`OPENAI_WEBHOOK_SECRET`, 5-minute tolerance) are
  rejected before anything else; re-deliveries are ignored.
- Rate limits: `WEBHOOK_REQUESTS_PER_IP_PER_MINUTE` per IP and `WEBHOOK_REQUESTS_PER_MINUTE`
  in total on `/openai/webhook` (HTTP 429, before the signature check), at most
  `CALLS_PER_CALLER` calls per number within `CALLS_PER_CALLER_WINDOW_SECONDS` (busy;
  numbers in `do_not_answer.allow` are exempt), and `MAX_CONCURRENT_CALLS` at once (busy).
  Callers with a hidden number share one limit.

---

## 3. GSM conditional call forwarding

Your mobile stays your number. Calls you do not take are forwarded by your operator to the
Telnyx number, and Jarvis answers them. Use **conditional** forwarding only: unconditional
forwarding (`**21*`, and the iPhone *Settings → Phone → Call Forwarding* switch) sends every
call to the agent, and your phone never rings.

### 3.1 Codes

Type the code in the phone app and press call. Use the full international form of the Telnyx
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

Caller ID: Telnyx normally receives the original caller's number, so the rules in
`rules.yaml` work. If every call shows your own mobile number instead, your operator replaces
it on forwarding; ask them about it.

### 3.4 Charges

**The forwarded leg is billed to you by your operator**, as a call from your phone to the
Telnyx number:

- Telnyx number in Lithuania: a domestic call, usually included in your plan.
- Telnyx number abroad (e.g. a US number): an **international call per forwarded minute**,
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

Only one process may receive the bot's updates. Stop the local app when
the deployed app is running, or you will see `Conflict: terminated by other getUpdates`
errors in the logs.

---

## 5. Cost per call minute

Everything is billed by the minute or by usage:

```
cost/min = T_in + VOICE + BACKEND
VOICE    = $0.05 per minute of GPT-Live session, billed per second
BACKEND  = backend (Responses) tokens: input × P_in + cached input × P_cached + output × P_out
```

| Term | What | Price (check the linked page) |
| --- | --- | --- |
| `T_in` | Telnyx inbound minute on your number (SIP trunking is billed per minute with the number; check whether the connection adds an item) | [Telnyx pricing](https://telnyx.com/pricing) (Lithuania, SIP trunking) |
| `VOICE` | GPT-Live listens and speaks for the **whole** session, hold included | $0.05/min ([gpt-live-1](https://developers.openai.com/api/docs/models/gpt-live-1)) |
| `BACKEND` | Only when GPT-Live delegates (decisions, function calls): a few short responses per call | gpt-6-luna: $0.10 per 1M input, $0.01 per 1M cached input, $0.50 per 1M output ([gpt-6-luna](https://developers.openai.com/api/docs/models/gpt-6-luna)) |

The voice minute dominates: a 2-minute call is $0.10 of voice plus well under a cent of backend
tokens, plus Telnyx's per-minute charge. The post-call summary is one more small backend
request per call.

Telnyx's Mission Control billing and usage pages show what real calls cost on its side.

**Not in the per-minute formula:**

- Your operator's charge for the forwarded leg (section 3.4). It is zero with a domestic number
  and an unlimited plan, but it can be the biggest item with a foreign number.
- Monthly fixed costs: the Telnyx number (monthly price shown when you buy it), hosting
  (Fly.io `shared-cpu-1x` 512 MB plus a 1 GB volume; Render Starter plus disk), and the
  document verification (no extra fee known; check at checkout).
- Telegram is free.

Prices change. Check the pricing pages before you rely on the numbers.
