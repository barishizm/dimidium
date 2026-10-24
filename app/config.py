"""Application settings loaded from environment variables / .env."""

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# What callers get in "off" answer mode: the agent takes a voicemail, or the call is rejected.
OffModeAction = Literal["voicemail", "reject"]
# Values accepted by delegation.responses in the openai SDK; the backend model may support fewer.
LiveReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh"]
LiveServiceTier = Literal["auto", "default", "flex", "priority"]
SummaryProvider = Literal["openai", "anthropic"]


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
    # Your real phone number (E.164): never spoken (checked in code with PRIVATE_TERMS).
    # OWNER_FORWARD_NUMBER is the old name of the same setting.
    owner_phone_number: str | None = Field(
        default=None,
        validation_alias=AliasChoices("owner_phone_number", "owner_forward_number"),
    )
    # Comma-separated private details the agent must never speak (street, city district,
    # workplace, family names...). The owner's numbers are always included. Matched in code.
    private_terms: str = ""

    # Post-call summary for the owner: a separate, cheap call after the phone call has ended.
    # "openai": Responses API with SUMMARY_MODEL (empty = LIVE_BACKEND_MODEL). "anthropic":
    # Claude with ANTHROPIC_API_KEY (pip install anthropic; empty model = claude-haiku-5-5).
    summary_provider: SummaryProvider = "openai"
    summary_model: str | None = None
    summary_timeout_seconds: float = Field(default=30, gt=0)
    anthropic_api_key: SecretStr | None = None

    # OpenAI GPT-Live (voice) with Responses delegation (backend)
    openai_api_key: SecretStr | None = None
    live_voice_model: str = "gpt-live-1"
    live_backend_model: str = "gpt-6-luna"
    live_backend_reasoning_effort: LiveReasoningEffort = "low"
    # Unset follows the project's default tier; "priority" is Fast mode where available.
    live_backend_service_tier: LiveServiceTier | None = None
    live_voice: str = "marin"
    # Lithuanian opening line (AI disclosure + recording notice). Empty = the reviewed default
    # from app/phrases_lt.py.
    live_greeting: str | None = None
    # Signing secret of the project webhook (whsec_...) that receives live.transport.incoming.
    # Without it every incoming-call webhook is refused.
    openai_webhook_secret: SecretStr | None = None
    # proj_... ID; only used by setup checks (the SIP URI is sip:<project id>@sip.api.openai.com).
    openai_project_id: str | None = None
    # Timeout for accept / reject / hangup requests.
    live_control_timeout_seconds: float = Field(default=10, gt=0)
    # The disclosure must appear in the agent's speech within this time, else it is re-sent.
    live_disclosure_deadline_seconds: float = Field(default=10, gt=0)
    # After end_call: hang up once the agent has been quiet this long (the goodbye finished),
    # and at the latest after live_goodbye_max_seconds.
    live_goodbye_quiet_seconds: float = Field(default=1.5, gt=0)
    live_goodbye_max_seconds: float = Field(default=10, gt=0)
    # After a backend or sideband failure the caller gets this long to leave a message.
    live_fallback_grace_seconds: float = Field(default=30, gt=0)

    # Telegram
    telegram_bot_token: SecretStr | None = None
    telegram_owner_chat_id: int | None = None

    # Twilio
    twilio_account_sid: str | None = None
    twilio_auth_token: SecretStr | None = None
    twilio_phone_number: str | None = None

    # Server and abuse limits
    port: int = Field(default=8000, gt=0)
    # On SIGTERM, active calls get this long to hear the goodbye before connections close.
    # Keep it below the platform's kill timeout (Fly kill_timeout, Render maxShutdownDelaySeconds).
    shutdown_grace_seconds: float = Field(default=20, ge=0)
    # POST /openai/webhook requests per minute from one client IP, and from everyone together.
    # Checked before the signature (cheap), so a flood never reaches the HMAC or the screening.
    webhook_requests_per_ip_per_minute: int = Field(default=60, gt=0)
    webhook_requests_per_minute: int = Field(default=300, gt=0)
    # Calls one number may make within the window before getting a busy signal
    # (allowlisted numbers are exempt).
    calls_per_caller: int = Field(default=4, gt=0)
    calls_per_caller_window_seconds: float = Field(default=600, gt=0)
    # Calls answered at the same time; more ring busy.
    max_concurrent_calls: int = Field(default=3, gt=0)

    # Call flow limits and timing
    max_call_seconds: float = Field(default=300, gt=0)
    # The agent is told to wrap up this long before max_call_seconds.
    wrap_up_seconds: float = Field(default=45, ge=0)
    hold_phrase_interval_seconds: float = Field(default=20, gt=0)
    # GPT-Live: "still trying to reach him" lines per hold, one per interval.
    hold_still_trying_max: int = Field(default=3, ge=0)
    # ask_owner calls allowed per call; later ones are refused without asking.
    max_owner_questions: int = Field(default=2, ge=1)

    @property
    def private_term_list(self) -> list[str]:
        return [term.strip() for term in self.private_terms.split(",") if term.strip()]

    def secret_values(self) -> list[str]:
        """Every configured secret, so logging can mask them wherever they appear."""
        secrets = [
            self.anthropic_api_key,
            self.openai_api_key,
            self.openai_webhook_secret,
            self.telegram_bot_token,
            self.twilio_auth_token,
        ]
        return [s.get_secret_value() for s in secrets if s is not None and s.get_secret_value()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
