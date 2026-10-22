import pytest

from app.config import Settings


def test_empty_env_values_are_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "")
    monkeypatch.setenv("HOLD_TIMEOUT_SECONDS", "")
    settings = Settings()
    assert settings.telegram_owner_chat_id is None
    assert settings.hold_timeout_seconds == 75
