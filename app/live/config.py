"""GPT-Live session configuration: voice prompt, Responses delegation, our four functions.

The same session body is used for a WebSocket `session.start` (smoke test, audio bridge) and
for accepting an inbound SIP call (`POST /v1/live/sessions/{id}/accept`); only the transport
details differ. Field names follow openai.types.live.SessionConfigParam / SessionAcceptParams.
"""

from typing import Any, Literal

from app.agent.tools import TOOL_DEFINITIONS
from app.config import Settings
from app.live.prompts import (
    VOICE_PROMPT,
    build_backend_prompt,
    default_greeting,
    greeting_instruction,
)

Transport = Literal["websocket", "sip"]

# Raw mono PCM16 at 24 kHz: the WebSocket default and the format of reflected sideband audio.
WEBSOCKET_AUDIO_FORMAT: dict[str, Any] = {"type": "audio/pcm", "rate": 24000}

LIVE_FUNCTION_NAMES = frozenset({"ask_owner", "save_message", "flag_suspicious", "end_call"})


def function_tools() -> list[dict[str, Any]]:
    """Our four functions as Responses function tools, built from the shared JSON schemas.

    Not strict: save_message has optional fields, and every call is validated in code with the
    Pydantic models in app.agent.tools anyway.
    """
    tools = [
        {
            "type": "function",
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["input_schema"],
            "strict": False,
        }
        for tool in TOOL_DEFINITIONS
    ]
    names = {tool["name"] for tool in tools}
    if names != LIVE_FUNCTION_NAMES:
        raise RuntimeError(f"unexpected tool set for GPT-Live: {sorted(names)}")
    return tools


def build_delegation(settings: Settings, rules_summary: str) -> dict[str, Any]:
    responses: dict[str, Any] = {
        "model": settings.live_backend_model,
        "instructions": build_backend_prompt(
            settings.owner_name, settings.hold_timeout_seconds, rules_summary
        ),
        # Function tools only: no web search or other hosted tools.
        "tools": function_tools(),
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "reasoning": {"effort": settings.live_backend_reasoning_effort},
    }
    if settings.live_backend_service_tier is not None:
        responses["service_tier"] = settings.live_backend_service_tier
    return {"type": "responses", "responses": responses}


def build_session_config(
    settings: Settings, rules_summary: str, transport: Transport, greeting: str | None = None
) -> dict[str, Any]:
    """Session object for `session.start` (websocket) or the SIP accept body's `session`.

    `greeting` overrides the configured opening line (voicemail-only calls).
    """
    greeting = greeting or resolve_greeting(settings)
    audio: dict[str, Any] = {"output": {"voice": settings.live_voice}}
    session: dict[str, Any] = {
        "model": settings.live_voice_model,
        "instructions": VOICE_PROMPT,
        "audio": audio,
        "delegation": build_delegation(settings, rules_summary),
        # Greeting instructions available from the first moment (live-conversations guide).
        "input": [
            {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": greeting_instruction(greeting)}],
            }
        ],
        # No stored recordings: raw audio is never kept (GDPR, see retention settings).
        "store": False,
    }
    if transport == "sip":
        # SIP negotiates the audio format; the accept body needs the session type.
        session["type"] = "live"
    else:
        audio["format"] = dict(WEBSOCKET_AUDIO_FORMAT)
    return session


def build_sip_accept_body(
    settings: Settings, rules_summary: str, greeting: str | None = None
) -> dict[str, Any]:
    return {"session": build_session_config(settings, rules_summary, "sip", greeting)}


def resolve_greeting(settings: Settings) -> str:
    return settings.live_greeting or default_greeting()
