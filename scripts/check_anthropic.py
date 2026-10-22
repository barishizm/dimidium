"""Verify the Anthropic API key in .env with one tiny request.

Usage: python scripts/check_anthropic.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import anthropic  # noqa: E402

from app.config import get_settings  # noqa: E402


def main() -> int:
    settings = get_settings()
    if settings.anthropic_api_key is None:
        print("FAIL: ANTHROPIC_API_KEY is empty in .env")
        return 1
    client = anthropic.Anthropic(
        api_key=settings.anthropic_api_key.get_secret_value(), timeout=30, max_retries=0
    )
    try:
        response = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=256,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": "Reply with the single word: OK"}],
        )
    except anthropic.AuthenticationError:
        print("FAIL: the key was rejected (401). Copy it again from the Console.")
        return 1
    except anthropic.PermissionDeniedError:
        print("FAIL: the key has no access (403). Check the workspace and billing.")
        return 1
    except anthropic.NotFoundError:
        print(f"FAIL: model {settings.anthropic_model!r} not found. Check ANTHROPIC_MODEL.")
        return 1
    except anthropic.APIStatusError as exc:
        print(f"FAIL: API error {exc.status_code}: {exc.message}")
        return 1
    except anthropic.APIConnectionError:
        print("FAIL: could not reach api.anthropic.com (network or timeout).")
        return 1
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    print(f"OK: {response.model} answered {text!r} ({response.usage.output_tokens} output tokens)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
