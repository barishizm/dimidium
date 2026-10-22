"""Standard Lithuanian phrases the agent speaks verbatim (fixed prompts, TTS benchmark).

NATIVE-SPEAKER REVIEW NEEDED: written as natural, polite standard Lithuanian (formal "jūs"),
but not yet checked by a native speaker. The owner is referred to as "jis" (he) and
"telefono savininkas" (the phone's owner) so no name has to be declined into Lithuanian cases.
Set `reviewed=True` on a phrase once a native speaker has approved it.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Phrase:
    key: str
    text: str
    english: str
    reviewed: bool = False


PHRASES: tuple[Phrase, ...] = (
    Phrase(
        "greeting",
        "Laba diena! Atsiliepia dirbtinio intelekto asistentas, nes telefono savininkas "
        "šiuo metu negali kalbėti. Pokalbis įrašomas ir transkribuojamas. Kuo galiu padėti?",
        "Good afternoon! An artificial intelligence assistant is answering because the phone's "
        "owner cannot talk right now. The call is recorded and transcribed. How can I help?",
    ),
    Phrase(
        "ask_name_reason",
        "Ar galėtumėte pasakyti savo vardą ir kokiu reikalu skambinate?",
        "Could you tell me your name and what you are calling about?",
    ),
    Phrase(
        "hold",
        "Prašau trumpam palaukti, nepadėkite ragelio. Tuoj pasitikslinsiu.",
        "Please hold for a short moment, do not hang up. I will check right away.",
    ),
    Phrase(
        "still_trying",
        "Vis dar bandau su juo susisiekti, prašau dar šiek tiek palaukti.",
        "I am still trying to reach him, please wait a little longer.",
    ),
    Phrase(
        "unavailable_take_message",
        "Deja, jis šiuo metu negali atsiliepti. Galiu užrašyti jūsų žinutę ir ją perduosiu.",
        "Unfortunately he cannot answer right now. I can take your message and pass it on.",
    ),
    Phrase(
        "message_taken",
        "Jūsų žinutę užrašiau ir perduosiu jam kuo greičiau.",
        "I have written down your message and will pass it on to him as soon as possible.",
    ),
    Phrase(
        "repeat",
        "Atsiprašau, neišgirdau. Ar galėtumėte pakartoti?",
        "Sorry, I did not catch that. Could you repeat?",
    ),
    Phrase(
        "refuse_sensitive",
        "Atsiprašau, bet negaliu priimti ar perduoti jokių kodų, slaptažodžių ar mokėjimo "
        "duomenų.",
        "Sorry, but I cannot accept or share any codes, passwords or payment details.",
    ),
    Phrase(
        "technical_issue",
        "Atsiprašau, kilo techninių nesklandumų. Galiu užrašyti jūsų žinutę ir ją perduosiu.",
        "Sorry, there was a technical problem. I can take your message and pass it on.",
    ),
    Phrase(
        "goodbye",
        "Ačiū už skambutį. Viso gero!",
        "Thank you for calling. Goodbye!",
    ),
    # Live-call phrases: fillers against silence, and fallbacks that must work without the LLM.
    Phrase(
        "filler",
        "Minutėlę.",
        "One moment.",
    ),
    Phrase(
        "filler_long",
        "Atsiprašau, dar akimirką.",
        "Sorry, just a moment more.",
    ),
    Phrase(
        "are_you_there",
        "Ar jūs dar girdite mane?",
        "Can you still hear me?",
    ),
    Phrase(
        "cant_hear",
        "Atsiprašau, dėl techninių nesklandumų negaliu jūsų girdėti. Apie jūsų skambutį "
        "pranešiu telefono savininkui.",
        "Sorry, because of a technical problem I cannot hear you. I will tell the phone's owner "
        "about your call.",
    ),
    Phrase(
        "time_limit",
        "Atsiprašau, turime baigti pokalbį. Viską, ką pasakėte, perduosiu.",
        "Sorry, we have to end the call. I will pass on everything you said.",
    ),
)

PHRASES_BY_KEY: dict[str, Phrase] = {phrase.key: phrase for phrase in PHRASES}
