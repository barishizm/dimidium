"""Standard Lithuanian phrases the agent is told to say verbatim (GPT-Live speaks them).

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
    # Voicemail mode (answer mode "off" / unknown caller in "known numbers only"): the owner is
    # not asked, the agent only takes a message.
    Phrase(
        "greeting_voicemail",
        "Laba diena! Atsiliepia dirbtinio intelekto asistentas. Telefono savininkas šiuo metu "
        "neatsiliepia. Pokalbis įrašomas ir transkribuojamas. Prašau pasakyti savo vardą ir "
        "kokiu reikalu skambinate, aš perduosiu jūsų žinutę.",
        "Good afternoon! An artificial intelligence assistant is answering. The phone's owner "
        "is not taking calls right now. The call is recorded and transcribed. Please tell me "
        "your name and what you are calling about, and I will pass on your message.",
    ),
    # GPT-Live: re-sent when the AI + recording disclosure is missing from the agent's speech.
    Phrase(
        "disclosure",
        "Noriu informuoti, kad su jumis kalba dirbtinio intelekto asistentas, o pokalbis "
        "įrašomas ir transkribuojamas.",
        "I would like to let you know that you are talking to an artificial intelligence "
        "assistant, and the call is recorded and transcribed.",
    ),
    Phrase(
        "hold",
        "Prašau trumpam palaukti, nepadėkite ragelio. Tuoj pasitikslinsiu.",
        "Please hold for a short moment, do not hang up. I will check right away.",
    ),
    Phrase(
        "still_trying",
        "Vis dar bandau su juo susisiekti. Ačiū, kad laukiate.",
        "I am still trying to reach him. Thank you for waiting.",
    ),
    Phrase(
        "refuse_sensitive",
        "Atsiprašau, bet negaliu priimti ar perduoti jokių kodų, slaptažodžių ar mokėjimo "
        "duomenų.",
        "Sorry, but I cannot accept or share any codes, passwords or payment details.",
    ),
    # Spoken instead of a sentence the private-info guard blocked.
    Phrase(
        "private_info",
        "Atsiprašau, tokios informacijos negaliu suteikti.",
        "Sorry, I cannot give out that information.",
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
    Phrase(
        "time_limit",
        "Atsiprašau, turime baigti pokalbį. Viską, ką pasakėte, perduosiu.",
        "Sorry, we have to end the call. I will pass on everything you said.",
    ),
)

PHRASES_BY_KEY: dict[str, Phrase] = {phrase.key: phrase for phrase in PHRASES}
