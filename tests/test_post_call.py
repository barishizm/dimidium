from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

from app.models import CallSession, TranscriptTurn
from app.post_call import (
    MAX_TRANSCRIPT_CHARS,
    AnthropicSummarizer,
    CallFacts,
    CallSummary,
    ResponsesSummarizer,
    build_prompt,
    create_summarizer,
    format_report,
    needs_attention,
    render_transcript,
    store_text,
)

SUMMARY = CallSummary(
    who="Jonas, DPD kuryesi",
    why="Paket teslimi",
    decided="Kapıya bırakılsın",
    told_caller="Paketi kapıya bırakabilirsiniz",
    follow_up="Yok",
    needs_follow_up=False,
)
FACTS = CallFacts(
    end_reason="caller said goodbye",
    owner_decision="Leave the parcel at the door.",
    messages_left=0,
    flagged=False,
    message_only=False,
)


def make_session() -> CallSession:
    start = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
    session = CallSession(call_sid="CA" + "1" * 32, caller_number="+37060000000", started_at=start)
    session.ended_at = start + timedelta(seconds=95)
    session.transcript = [
        TranscriptTurn("agent", "Laba diena!"),
        TranscriptTurn("caller", "Čia Jonas iš DPD."),
    ]
    return session


class FakeParse:
    """Stands in for client.responses.parse / client.messages.parse."""

    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def responses_summarizer(result: Any) -> tuple[ResponsesSummarizer, FakeParse]:
    responses = FakeParse(result)
    client = SimpleNamespace(responses=responses)
    return ResponsesSummarizer(client, "gpt-6-luna"), responses  # type: ignore[arg-type]


def parsed(status: str = "completed", output: CallSummary | None = SUMMARY) -> SimpleNamespace:
    return SimpleNamespace(status=status, output_parsed=output)


async def test_summarize_returns_parsed_output() -> None:
    summ, responses = responses_summarizer(parsed())
    assert await summ.summarize(make_session(), FACTS) == SUMMARY
    call = responses.calls[0]
    assert call["model"] == "gpt-6-luna"
    assert call["text_format"] is CallSummary
    # Personal data: nothing kept as application state at OpenAI.
    assert call["store"] is False
    # The transcript is fenced as data, after the system facts.
    content = call["input"]
    assert "<transcript>\nASİSTAN: Laba diena!\nARAYAN: Čia Jonas iš DPD.\n</transcript>" in content
    assert "Leave the parcel at the door." in content


@pytest.mark.parametrize(
    "result",
    [
        parsed(status="incomplete"),
        parsed(output=None),
        openai.APIConnectionError(request=httpx.Request("POST", "https://x")),
        RuntimeError("boom"),
    ],
)
async def test_summarize_failures_return_none(result: Any) -> None:
    summ, _ = responses_summarizer(result)
    assert await summ.summarize(make_session(), FACTS) is None


async def test_summarize_skips_calls_without_caller_speech() -> None:
    summ, responses = responses_summarizer(parsed())
    session = make_session()
    session.transcript = [TranscriptTurn("agent", "Laba diena!")]
    assert await summ.summarize(session, FACTS) is None
    assert responses.calls == []


async def test_anthropic_summarizer_still_works() -> None:
    pytest.importorskip("anthropic")
    messages = FakeParse(SimpleNamespace(stop_reason="end_turn", parsed_output=SUMMARY))
    summ = AnthropicSummarizer(SimpleNamespace(messages=messages), "claude-haiku-5-5")
    assert await summ.summarize(make_session(), FACTS) == SUMMARY
    assert messages.calls[0]["output_format"] is CallSummary


def test_create_summarizer_picks_provider_and_model() -> None:
    from app.config import Settings

    assert create_summarizer(Settings(_env_file=None)) is None  # type: ignore[call-arg]
    default = create_summarizer(Settings(_env_file=None, openai_api_key="sk-x"))  # type: ignore[call-arg]
    assert isinstance(default, ResponsesSummarizer) and default.model == "gpt-6-luna"
    no_key = Settings(_env_file=None, openai_api_key="sk-x", summary_provider="anthropic")  # type: ignore[call-arg]
    assert create_summarizer(no_key) is None


def test_long_transcript_keeps_both_ends() -> None:
    session = make_session()
    session.transcript = [TranscriptTurn("caller", "x" * 100) for _ in range(500)]
    session.transcript.append(TranscriptTurn("agent", "Viso gero!"))
    rendered = render_transcript(session)
    assert len(rendered) <= MAX_TRANSCRIPT_CHARS + 10
    assert "[...]" in rendered and rendered.endswith("ASİSTAN: Viso gero!")


def test_prompt_mentions_voicemail_mode() -> None:
    facts = CallFacts(None, None, 1, False, True)
    assert "voicemail only" in build_prompt(make_session(), facts)


def test_report_with_summary() -> None:
    text = format_report("#1111 · +370", make_session(), FACTS, SUMMARY, {"turns": 0})
    lines = text.split("\n")
    assert lines[0] == "📴 Arama bitti #1111 · +370"
    assert "👤 Kim: Jonas, DPD kuryesi" in lines
    assert "➡️ Takip: Yok" in lines
    assert "Süre: 1:35 · Konuşma: 2 satır" in lines


def test_report_without_summary_falls_back_to_facts() -> None:
    session = make_session()
    session.owner_decision = "Leave it."
    facts = CallFacts("timeout", "Leave it.", 2, True, False)
    text = format_report("#1111", session, facts, None)
    assert text.startswith("🚩 Arama bitti")
    assert "Karar: Leave it." in text and "📝 Bırakılan mesaj: 2" in text


@pytest.mark.parametrize(
    ("facts", "summary", "expected"),
    [
        (FACTS, SUMMARY, False),
        (FACTS, None, False),
        (FACTS, SUMMARY.model_copy(update={"needs_follow_up": True}), True),
        (CallFacts(None, None, 1, False, False), SUMMARY, True),
        (CallFacts(None, None, 0, True, False), SUMMARY, True),
        (CallFacts(None, None, 0, False, False, hung_up_on_hold=True), None, True),
    ],
)
def test_needs_attention(facts: CallFacts, summary: CallSummary | None, expected: bool) -> None:
    assert needs_attention(facts, summary) is expected


def test_store_text() -> None:
    assert store_text(None) is None
    stored = store_text(SUMMARY)
    assert stored is not None and stored.startswith("Jonas, DPD kuryesi — Paket teslimi")
