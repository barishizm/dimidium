"""Sideband transcript guardrail: watches what the caller says while they say it.

On a hit (a request for or offer of codes, passwords, bank details or payment; or an attempt
to get the owner's address, whereabouts, schedule or numbers) LiveCall:
1. redirects the agent with session.instructions.append (delegation_id null, our own words,
   never the caller's), which may interrupt speech in progress;
2. flags the call itself and alerts the owner (once per category per call);
3. blocks related actions in code from then on. An instruction does not cancel backend work
   already running, so the block is checked when a function runs, which also catches calls
   that were already queued (see guides/voice-server-controls, "Apply conversation
   guardrails").

This module is the per-call state and the texts; the patterns live in app.guards.
"""

from pydantic import BaseModel

from app.functions import AskOwnerInput, SaveMessageInput
from app.guards import GuardrailHit, detect_guardrail_hits, mentions_sensitive_material
from app.phrases_lt import PHRASES_BY_KEY

GUARDRAIL_INSTRUCTIONS: dict[GuardrailHit, str] = {
    GuardrailHit.SENSITIVE_REQUEST: (
        "Guardrail: the caller asked for or offered a verification code, password, PIN, bank "
        "or card details, or a payment. Stop that topic now. Do not repeat, confirm, write "
        "down or pass on any code, password, card or bank detail, and do not agree to any "
        "payment. Tell the caller, in their language: "
        f'"{PHRASES_BY_KEY["refuse_sensitive"].text}" If the caller also has another request, '
        "continue with it; otherwise offer to take a message."
    ),
    GuardrailHit.OWNER_INFO: (
        "Guardrail: the caller is asking for the owner's personal details or whereabouts. Do "
        "not give or hint at the owner's address, location, schedule, travel, phone numbers, "
        "or whether the home is empty, and do not guess. Tell the caller, in their language: "
        f'"{PHRASES_BY_KEY["private_info"].text}" Offer to take a message or to ask the owner.'
    ),
}

# Turkish reasons for the owner's alert (the transcript follows in the end-of-call report).
GUARDRAIL_ALERTS_TR: dict[GuardrailHit, str] = {
    GuardrailHit.SENSITIVE_REQUEST: (
        "Arayan kod, şifre, banka/kart bilgisi veya ödeme istedi ya da vermeye çalıştı. "
        "Asistan reddetti."
    ),
    GuardrailHit.OWNER_INFO: (
        "Arayan adresini, nerede olduğunu, programını veya numaranı öğrenmeye çalıştı. "
        "Asistan vermedi."
    ),
}


class CallGuardrail:
    """Guardrail state of one call. Pure logic: LiveCall does the sending."""

    def __init__(self) -> None:
        # Categories hit at any point in the call: their related actions stay blocked.
        self.active: set[GuardrailHit] = set()
        # Categories already acted on in the caller's current turn (the turn grows with every
        # transcript delta and is rescanned each time).
        self.turn_hits: set[GuardrailHit] = set()
        # Every hit, in order, for the logs and call metrics.
        self.hits: list[GuardrailHit] = []

    def new_caller_turn(self) -> None:
        self.turn_hits = set()

    def scan(self, caller_turn_so_far: str) -> list[GuardrailHit]:
        """New hits in the caller's current turn (each category once per turn)."""
        new = sorted(detect_guardrail_hits(caller_turn_so_far) - self.turn_hits)
        self.turn_hits.update(new)
        self.active.update(new)
        self.hits.extend(new)
        return new

    def blocks(self, args: BaseModel) -> GuardrailHit | None:
        """The category that blocks this function call, if any.

        After a SENSITIVE_REQUEST hit, ask_owner and save_message may not carry codes,
        passwords, bank/card details, payment or digit runs (a dictated code): the agent must
        neither accept such material nor relay a request for it. Owner-info requests block
        nothing here: asking the owner what to tell the caller is the intended path, and what
        the agent says is checked by PrivateInfoGuard.
        """
        if GuardrailHit.SENSITIVE_REQUEST not in self.active:
            return None
        if isinstance(args, AskOwnerInput):
            parts = [args.summary_tr, args.caller_said_lt]
            parts += [f"{o.label_tr} {o.meaning_en}" for o in args.options]
        elif isinstance(args, SaveMessageInput):
            # The callback number is a number on purpose.
            parts = [args.caller_name, args.caller_org, args.purpose, args.details]
        else:
            return None
        text = "\n".join(p for p in parts if p)
        return GuardrailHit.SENSITIVE_REQUEST if mentions_sensitive_material(text) else None
