"""Call-screening rules loaded from rules.yaml, summarized into the agent's per-call context."""

import logging
import re
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)


class ContactAction(StrEnum):
    NOTIFY_IMMEDIATELY = "notify_immediately"
    SCREEN = "screen"
    POLITE_REJECT = "polite_reject"


class _Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_number(value: str) -> str:
    if len(normalize_number(value)) < 6:
        raise ValueError(f"not a phone number: {value!r}")
    return value


class KnownContact(_Rule):
    number: str
    name: str
    relation: str = "known"
    action: ContactAction = ContactAction.NOTIFY_IMMEDIATELY

    _valid_number = field_validator("number")(_check_number)


class AllowedContact(KnownContact):
    """Do-not-answer exception: always gets through (whatever the answer mode) and the owner is
    alerted the moment the phone rings."""

    # Turkish Telegram alert sent on ring, e.g. "Agate arıyor". Default: "<name> arıyor".
    alert_tr: str | None = None
    # Offer the owner a button that forwards the live call to OWNER_FORWARD_NUMBER.
    offer_forward: bool = False

    @property
    def alert_text(self) -> str:
        return self.alert_tr or f"{self.name} arıyor"


class BlockedNumber(_Rule):
    """Rejected before the agent answers (busy signal, no AI cost, no notification)."""

    number: str
    note: str = ""

    _valid_number = field_validator("number")(_check_number)


class DoNotAnswerLists(_Rule):
    allow: list[AllowedContact] = Field(default_factory=list)
    deny: list[BlockedNumber] = Field(default_factory=list)


class KeywordRule(_Rule):
    name: str
    keywords: list[str] = Field(min_length=1)
    action: ContactAction = ContactAction.SCREEN
    allowed_actions: list[str] = Field(default_factory=list)
    guidance: str = ""


class RulesConfig(_Rule):
    known_numbers: list[KnownContact] = Field(default_factory=list)
    keyword_rules: list[KeywordRule] = Field(default_factory=list)
    do_not_answer: DoNotAnswerLists = Field(default_factory=DoNotAnswerLists)
    unknown_caller_guidance: str = (
        "Unknown caller: screen the call, gather details, then ask the owner."
    )


def normalize_number(number: str) -> str:
    """Digits only, with a Lithuanian national prefix (8 / 0) mapped to 370."""
    digits = re.sub(r"\D", "", number)
    if digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("8") and len(digits) == 9:
        digits = "370" + digits[1:]
    return digits


class RulesEngine:
    def __init__(self, config: RulesConfig) -> None:
        self.config = config
        self._by_number: dict[str, KnownContact] = {
            normalize_number(c.number): c for c in config.known_numbers
        }
        # Allowlist entries are known contacts too, and win over a duplicate known_numbers entry.
        self._by_number.update({normalize_number(c.number): c for c in config.do_not_answer.allow})
        self._blocked = {normalize_number(b.number) for b in config.do_not_answer.deny}

    @classmethod
    def from_yaml(cls, path: str | Path) -> "RulesEngine":
        path = Path(path)
        if not path.exists():
            logger.warning("Rules file %s not found; using empty rules", path)
            return cls(RulesConfig())
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(RulesConfig.model_validate(data))

    def match_number(self, caller_number: str | None) -> KnownContact | None:
        if not caller_number:
            return None
        return self._by_number.get(normalize_number(caller_number))

    def allowed(self, caller_number: str | None) -> AllowedContact | None:
        contact = self.match_number(caller_number)
        return contact if isinstance(contact, AllowedContact) else None

    def is_blocked(self, caller_number: str | None) -> bool:
        """Denylisted. An allowlist entry for the same number wins."""
        if not caller_number or self.allowed(caller_number) is not None:
            return False
        return normalize_number(caller_number) in self._blocked

    def match_keywords(self, text: str) -> list[KeywordRule]:
        lowered = text.casefold()
        return [
            rule
            for rule in self.config.keyword_rules
            if any(kw.casefold() in lowered for kw in rule.keywords)
        ]

    def summary_for_call(self, caller_number: str | None) -> str:
        """Plain-English rules summary injected into the system prompt for one call.

        Never includes the caller's or any known contact's phone number.
        """
        lines: list[str] = []
        contact = self.match_number(caller_number)
        if contact is None:
            status = "not in contacts" if caller_number else "withheld"
            lines.append(f"- Caller number: {status}.")
            lines.append(f"- {self.config.unknown_caller_guidance}")
        elif contact.action is ContactAction.NOTIFY_IMMEDIATELY:
            lines.append(
                f"- Caller number belongs to {contact.name} ({contact.relation}), a trusted "
                "contact. Do not screen. Greet them by name, ask briefly what they need, and "
                "call ask_owner right away. Still never share codes, passwords or bank details."
            )
        elif contact.action is ContactAction.POLITE_REJECT:
            lines.append(
                f"- Caller number belongs to {contact.name} ({contact.relation}). The owner does "
                "not want to talk to them: politely say the owner is unavailable and end the call."
            )
        else:
            lines.append(f"- Caller number belongs to {contact.name} ({contact.relation}). Screen.")

        if self.config.keyword_rules:
            lines.append("- Caller types (decide from what the caller says):")
            for rule in self.config.keyword_rules:
                parts = [f"  * {rule.name} (e.g. {', '.join(rule.keywords[:6])}): {rule.action}."]
                if rule.allowed_actions:
                    parts.append(f"Options to offer the owner: {'; '.join(rule.allowed_actions)}.")
                if rule.guidance:
                    parts.append(rule.guidance)
                lines.append(" ".join(parts))
        return "\n".join(lines)
