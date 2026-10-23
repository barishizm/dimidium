import pytest
from pydantic import ValidationError

from app.config import Settings
from app.live.config import LIVE_FUNCTION_NAMES, build_session_config, build_sip_accept_body
from app.live.prompts import VOICE_PROMPT, default_greeting


def _settings(**overrides: object) -> Settings:
    return Settings(owner_name="Test Owner", **overrides)  # type: ignore[arg-type]


def test_websocket_session_defaults() -> None:
    session = build_session_config(_settings(), "- Caller number: withheld.", "websocket")

    assert session["model"] == "gpt-live-1"
    assert session["instructions"] == VOICE_PROMPT
    assert session["audio"] == {
        "format": {"type": "audio/pcm", "rate": 24000},
        "output": {"voice": "marin"},
    }
    assert session["store"] is False
    assert "type" not in session

    delegation = session["delegation"]
    assert delegation["type"] == "responses"
    responses = delegation["responses"]
    assert responses["model"] == "gpt-6-luna"
    assert responses["tool_choice"] == "auto"
    assert responses["parallel_tool_calls"] is False
    assert responses["reasoning"] == {"effort": "low"}
    assert "service_tier" not in responses


def test_backend_prompt_gets_owner_timeout_and_rules() -> None:
    session = build_session_config(
        _settings(hold_timeout_seconds=60), "- Caller number: not in contacts.", "websocket"
    )
    instructions = session["delegation"]["responses"]["instructions"]

    assert "Test Owner" in instructions
    assert "within 60 seconds" in instructions
    assert "- Caller number: not in contacts." in instructions
    assert "{" not in instructions


def test_rules_summary_with_braces_is_inserted_verbatim() -> None:
    session = build_session_config(_settings(), "- Note: {not a placeholder}", "websocket")
    assert "{not a placeholder}" in session["delegation"]["responses"]["instructions"]


def test_only_our_four_function_tools_no_hosted_tools() -> None:
    tools = build_session_config(_settings(), "", "websocket")["delegation"]["responses"]["tools"]

    assert {tool["name"] for tool in tools} == LIVE_FUNCTION_NAMES
    for tool in tools:
        assert tool["type"] == "function"
        assert tool["parameters"]["type"] == "object"
        assert tool["parameters"]["additionalProperties"] is False
        assert "eager_input_streaming" not in tool


def test_env_overrides_models_effort_and_service_tier() -> None:
    settings = _settings(
        live_voice_model="gpt-live-2",
        live_backend_model="gpt-6-sol",
        live_backend_reasoning_effort="medium",
        live_backend_service_tier="priority",
        live_voice="cedar",
    )
    session = build_session_config(settings, "", "websocket")
    responses = session["delegation"]["responses"]

    assert session["model"] == "gpt-live-2"
    assert session["audio"]["output"] == {"voice": "cedar"}
    assert responses["model"] == "gpt-6-sol"
    assert responses["reasoning"] == {"effort": "medium"}
    assert responses["service_tier"] == "priority"


def test_invalid_reasoning_effort_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings(live_backend_reasoning_effort="turbo")


def test_sip_accept_body_has_type_and_no_audio_format() -> None:
    body = build_sip_accept_body(_settings(), "")
    session = body["session"]

    assert session["type"] == "live"
    assert session["audio"] == {"output": {"voice": "marin"}}


def test_greeting_is_configurable_and_defaults_to_reviewed_phrase() -> None:
    default = build_session_config(_settings(), "", "websocket")["input"][0]
    custom = build_session_config(_settings(live_greeting="Labas!"), "", "websocket")["input"][0]

    assert default["role"] == "developer"
    assert default_greeting() in default["content"][0]["text"]
    assert custom["content"][0]["text"].endswith("Labas!")


def test_session_validates_against_openai_sdk_types() -> None:
    from typing import get_type_hints

    from openai.types.live.session_accept_params import Session as AcceptSession
    from openai.types.live.session_config_param import SessionConfigParam

    ws = build_session_config(_settings(), "", "websocket")
    sip = build_sip_accept_body(_settings(), "")["session"]

    assert set(ws) <= set(get_type_hints(SessionConfigParam))
    assert set(sip) <= set(get_type_hints(AcceptSession))
