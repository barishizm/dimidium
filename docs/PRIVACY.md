# Privacy and data handling (GDPR)

What Jarvis does with callers' personal data, where it goes, and how long it stays. Sources:
OpenAI's [data controls](https://developers.openai.com/api/docs/guides/your-data),
[live-conversations](https://developers.openai.com/api/docs/guides/live-conversations) and
[live-delegation](https://developers.openai.com/api/docs/guides/live-delegation) guides (read
2026-10-24). This is an engineering summary, not legal advice.

## Who processes what

| Party | What it gets | Kept |
|---|---|---|
| **OpenAI** (GPT-Live voice session) | The caller's **audio**, both transcripts, the AI's speech, our prompts and the `rules.yaml` summary (no phone numbers) | Session `store: false`: no stored recording (see below). Abuse-monitoring logs up to 30 days unless the project has Zero Data Retention or Modified Abuse Monitoring. |
| **OpenAI** (Responses backend, delegated by GPT-Live) | The conversation context the voice model hands over, function calls and results (owner decisions) | Responses has 30-day application state by default. The delegation config has **no `store` field**, so we cannot switch it off from the app (see "Open points"). |
| **OpenAI** (post-call summary) | The stored transcript and call facts | Sent with `store: false`. Abuse-monitoring logs as above. |
| **Twilio** | Call routing and media between the network and OpenAI (SIP/SRTP), caller ID, call records | Twilio's call logs (CDRs). Keep trunk recording **off** (it is off by default). |
| **Telegram** | Owner questions and reports: caller number, summary, what the caller said, messages left | In the owner's Telegram chat until deleted there. Bot chats are cloud chats, not end-to-end encrypted. |
| **This app** (SQLite on our volume) | Transcripts, call rows (number, times, owner decision, summary, flags), messages for the owner | Transcripts: `RETENTION_DAYS` (30). Call rows, summaries, messages: `RECORDS_RETENTION_DAYS` (365). Daily cleanup. |
| **Hosting logs** (Fly.io / Render) | JSON logs: call IDs, event types, timings, costs. Phone numbers are masked (`+370******67`), secrets masked, **no transcript text** | Platform log retention. |

## Audio

- **This app never receives or stores audio.** Twilio sends the call straight to OpenAI over
  SIP; the app only sees events and transcripts on the sideband.
- **OpenAI side:** the session config sets `store: false` explicitly
  ([app/live/config.py](../app/live/config.py), tested in `tests/test_live_config.py`). It is
  also the default. With `store: false` there is no stored recording, no forking, and
  `GET /v1/live/sessions/{id}/content` has nothing to return. The data-controls table lists
  `/v1/live/sessions` application state as "None, or 30 days if stored". That is the most
  privacy-preserving setting the session config offers.
- Audio is still **processed** by OpenAI to run the call, and its content can appear in
  OpenAI's abuse-monitoring logs (up to 30 days) unless the organization or project is
  approved for Zero Data Retention or Modified Abuse Monitoring. With Zero Data Retention,
  `store` is forced to `false` anyway.

## Disclosure (AI + recording)

- The opening line says the caller is talking to an AI assistant and that the call is recorded
  and transcribed ([app/phrases_lt.py](../app/phrases_lt.py), `greeting`). "Recorded" here means
  the transcript; the app stores no audio.
- Per call, the app checks the agent's own transcript for the disclosure. If it is missing
  after `LIVE_DISCLOSURE_DEADLINE_SECONDS`, the app makes the agent say it, and if it is still
  missing, the owner is alerted. The result is stored per call (`calls.disclosure_verified`,
  `calls.disclosure_resent`) as an audit trail.

## Data minimisation in the prompts

- No phone numbers go into any prompt. The rules summary names a known contact's name and
  relation only.
- `PRIVATE_TERMS` and the owner's numbers are never in a prompt. They are checked in code on
  the agent's speech, and removed from owner decisions before those go back to the model.
- The transcript guardrail stops requests for codes, passwords, bank details and payment, and
  requests for the owner's whereabouts ([docs/GPT_LIVE_DESIGN.md](GPT_LIVE_DESIGN.md) §7).

## Deleting one caller's data

The daily cleanup covers retention. For an erasure request, stop the app (or work on a copy),
then:

```bash
sqlite3 data/dimidium.db "PRAGMA foreign_keys = ON;
  DELETE FROM messages_for_owner WHERE call_sid IN
    (SELECT call_sid FROM calls WHERE caller_number = '+3706XXXXXXX');
  DELETE FROM calls WHERE caller_number = '+3706XXXXXXX';"   -- transcripts cascade
```

Then delete the matching Telegram messages by hand. Twilio call logs can be deleted in the
Twilio Console or through its API. OpenAI holds no stored session with `store: false`.

## Open points (owner decisions)

1. **Zero Data Retention or Modified Abuse Monitoring.** Both need OpenAI's approval (contact
   OpenAI sales/support for the organization). They are the only way to keep call content
   out of OpenAI's 30-day abuse-monitoring logs, and Zero Data Retention also forces the
   backend's Responses `store` to `false`.
2. **Backend Responses retention.** GPT-Live's delegation config exposes `model`,
   `instructions`, `tools`, `tool_choice`, `parallel_tool_calls`, `max_output_tokens`,
   `service_tier`, `reasoning` and `text`. There is no `store` field. Whether delegated
   responses keep the default 30-day application state is not stated in the guides
   (*unverified*). Ask OpenAI, or rely on item 1.
3. **EU data residency.** OpenAI offers EU residency for `/v1/live/sessions` and
   `/v1/responses` (an EU-region project, `sip-eu.api.openai.com` for SIP,
   `eu.api.openai.com` for the API). The app currently calls `api.openai.com`; switching
   needs a base-URL setting and a test of the sideband against the EU host. Not done.
4. **Telegram.** Owner reports contain personal data and stay in Telegram until deleted.
   Consider a periodic manual cleanup of the bot chat.
5. **Lawful basis and notice.** The greeting is a short notice. A fuller privacy notice
   (controller, purpose, retention, rights) may be needed, e.g. "more information:
   <link>" in the voicemail flow or on a web page. Confirm with whoever advises you on GDPR.
