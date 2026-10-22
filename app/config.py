"""Application settings loaded from environment variables / .env."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

SpeechProvider = Literal["google", "azure"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    log_level: str = "INFO"
    public_base_url: str = "http://localhost:8000"
    db_path: str = "data/dimidium.db"
    hold_timeout_seconds: float = Field(default=75, gt=0)

    # Anthropic
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-sonnet-5-5"

    # Telegram
    telegram_bot_token: SecretStr | None = None
    telegram_owner_chat_id: int | None = None

    # Twilio
    twilio_account_sid: str | None = None
    twilio_auth_token: SecretStr | None = None
    twilio_phone_number: str | None = None

    # Speech
    stt_provider: SpeechProvider = "google"
    tts_provider: SpeechProvider = "google"
    caller_language: str = "lt-LT"
    fallback_language: str = "en-US"

    google_application_credentials: str | None = None
    google_cloud_project: str | None = None
    google_stt_location: str = "eu"

    azure_speech_key: SecretStr | None = None
    azure_speech_region: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
