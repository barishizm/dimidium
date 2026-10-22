"""Application settings loaded from environment variables / .env."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

STTProviderName = Literal["google", "azure", "deepgram"]
# Deepgram Aura has no Lithuanian voice, so Deepgram is STT-only.
TTSProviderName = Literal["google", "azure", "elevenlabs"]
# What callers get in "off" answer mode: the agent takes a voicemail, or Twilio rejects (busy).
OffModeAction = Literal["voicemail", "reject"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True
    )

    # App
    log_level: str = "INFO"
    public_base_url: str = "http://localhost:8000"
    db_path: str = "data/dimidium.db"
    hold_timeout_seconds: float = Field(default=75, gt=0)
    owner_name: str = "the owner"
    rules_path: str = "rules.yaml"
    # Transcripts older than this are deleted by a daily cleanup. Raw audio is never stored.
    retention_days: int = Field(default=30, ge=1)
    off_mode_action: OffModeAction = "voicemail"
    # Your real phone number (E.164). Allowlisted callers can be forwarded here via <Dial>.
    owner_forward_number: str | None = None
    forward_ring_seconds: int = Field(default=20, ge=5, le=60)

    # Anthropic
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-haiku-5-5"
    anthropic_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    anthropic_max_tokens: int = Field(default=2048, gt=0)
    anthropic_timeout_seconds: float = Field(default=20, gt=0)
    # Server-side refusal fallback ("default" routing). Disable if the configured model rejects it.
    anthropic_refusal_fallback: bool = True
    # Post-call summary for the owner: a separate, cheap call after the phone call has ended.
    summary_model: str = "claude-haiku-5-5"
    summary_timeout_seconds: float = Field(default=30, gt=0)

    # Telegram
    telegram_bot_token: SecretStr | None = None
    telegram_owner_chat_id: int | None = None

    # Twilio
    twilio_account_sid: str | None = None
    twilio_auth_token: SecretStr | None = None
    twilio_phone_number: str | None = None
    # Reject /twilio/voice requests without a valid X-Twilio-Signature. Only turn off for
    # local curl tests; Twilio always signs its webhooks.
    twilio_validate_signature: bool = True

    # Server and abuse limits
    port: int = Field(default=8000, gt=0)
    # On SIGTERM, active calls get this long to hear the goodbye before connections close.
    # Keep it below the platform's kill timeout (Fly kill_timeout, Render maxShutdownDelaySeconds).
    shutdown_grace_seconds: float = Field(default=20, ge=0)
    # Requests per minute to /twilio/voice from one IP, checked before the signature.
    voice_requests_per_ip_per_minute: int = Field(default=60, gt=0)
    # Calls one number may make within the window before getting a busy signal
    # (allowlisted numbers are exempt).
    calls_per_caller: int = Field(default=4, gt=0)
    calls_per_caller_window_seconds: float = Field(default=600, gt=0)
    # Calls answered at the same time; more ring busy.
    max_concurrent_calls: int = Field(default=3, gt=0)

    # Call flow limits and timing
    max_call_seconds: float = Field(default=300, gt=0)
    max_caller_turns: int = Field(default=30, gt=0)
    # The agent is told to wrap up this long before max_call_seconds / 2 turns before the cap.
    wrap_up_seconds: float = Field(default=45, ge=0)
    # Speak a filler phrase if the agent has said nothing this long after the caller's turn.
    filler_after_seconds: float = Field(default=2.0, gt=0)
    # Fillers per turn before the turn counts as an LLM failure.
    max_fillers_per_turn: int = Field(default=2, ge=0)
    # Ask "are you still there?" after this much silence from the caller.
    caller_idle_seconds: float = Field(default=12, gt=0)
    hold_phrase_interval_seconds: float = Field(default=20, gt=0)
    # Your own hold music: 8 kHz mono mu-law (.wav or raw .ulaw). Empty = generated music.
    hold_audio_path: str | None = None
    # ask_owner calls allowed per call; later ones are refused without asking.
    max_owner_questions: int = Field(default=2, ge=1)
    # Turn detection: VAD silence before "stopped speaking", then the wait for more speech.
    vad_stop_secs: float = Field(default=0.3, gt=0)
    turn_speech_timeout: float = Field(default=0.5, ge=0)
    latency_target_ms: int = Field(default=1200, gt=0)
    # Pipecat's own log level (loguru). DEBUG logs transcripts and spoken text.
    pipecat_log_level: str = "WARNING"

    # Speech
    stt_provider: STTProviderName = "google"
    tts_provider: TTSProviderName = "google"
    caller_language: str = "lt-LT"
    fallback_language: str = "en-US"
    # Connect / first-response timeout for speech APIs.
    speech_timeout_seconds: float = Field(default=15, gt=0)
    # Comma-separated STT languages for the live call; empty means CALLER_LANGUAGE only.
    # Adding FALLBACK_LANGUAGE enables auto-detection where the provider supports it.
    stt_languages: str = ""
    # P99 time from end of speech to the final transcript; tunes Pipecat's turn detection.
    stt_final_p99_seconds: float = Field(default=1.0, gt=0)
    # A live TTS request that yields no audio within this time is retried once.
    tts_first_byte_timeout_seconds: float = Field(default=4.0, gt=0)
    # Pre-synthesized fixed phrases (greeting, fillers, fallbacks) are cached here.
    phrase_cache_dir: str = "data/phrase_cache"

    google_application_credentials: str | None = None
    google_cloud_project: str | None = None
    # chirp_3 serves lt-LT in the "eu"/"us" multi-regions; chirp and chirp_2 in europe-west4.
    google_stt_location: str = "eu"
    google_stt_model: str = "chirp_3"
    # Chirp3-HD voices stream; lt-LT-Standard-B is the only non-Chirp lt-LT voice.
    google_tts_voice: str = "lt-LT-Chirp3-HD-Kore"

    azure_speech_key: SecretStr | None = None
    azure_speech_region: str | None = None
    azure_tts_voice: str = "lt-LT-OnaNeural"

    deepgram_api_key: SecretStr | None = None
    deepgram_model: str = "nova-3"

    elevenlabs_api_key: SecretStr | None = None
    elevenlabs_voice_id: str | None = None
    # Only eleven_v3 / eleven_v4* list Lithuanian; flash_v2_5 and multilingual_v2 do not.
    elevenlabs_model: str = "eleven_v3"

    @property
    def stt_language_list(self) -> tuple[str, ...]:
        languages = tuple(code.strip() for code in self.stt_languages.split(",") if code.strip())
        return languages or (self.caller_language,)

    @property
    def public_ws_base_url(self) -> str:
        """PUBLIC_BASE_URL with the scheme switched to ws/wss."""
        base = self.public_base_url.rstrip("/")
        if base.startswith("https://"):
            return "wss://" + base.removeprefix("https://")
        if base.startswith("http://"):
            return "ws://" + base.removeprefix("http://")
        return base


@lru_cache
def get_settings() -> Settings:
    return Settings()
