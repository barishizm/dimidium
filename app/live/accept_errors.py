"""Turn a failed accept request into a clear cause, for the log and the owner's Telegram.

Pure logic. The cause is a hint, not a diagnosis: OpenAI documents `decision_already_made`
and `session_id_not_found` for call control; the 401/403 readings are the usual causes.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class AcceptFailure:
    key: str
    english: str  # log
    turkish: str  # Telegram (owner)


def explain_accept_failure(exc: BaseException) -> AcceptFailure:
    status = getattr(exc, "status_code", None)
    code = getattr(exc, "code", None)
    if code == "decision_already_made":
        return AcceptFailure(
            "already_decided",
            "the call was already accepted or rejected (another handler on the same webhook?)",
            "Arama zaten cevaplanmış veya reddedilmiş (aynı projede başka bir webhook var mı?).",
        )
    if code == "session_id_not_found" or status == 404:
        return AcceptFailure(
            "session_not_found",
            "session not found: the caller hung up already, or the API key, webhook and SIP "
            "destination belong to different OpenAI projects",
            "Oturum bulunamadı: arayan kapatmış olabilir; ya da API anahtarı, webhook ve "
            "Telnyx 'Translated Number' aynı OpenAI projesinde değil.",
        )
    if status == 401:
        return AcceptFailure(
            "bad_key",
            "OpenAI rejected OPENAI_API_KEY (401)",
            "OpenAI API anahtarı reddedildi (401). OPENAI_API_KEY'i kontrol edin.",
        )
    if status == 403:
        return AcceptFailure(
            "sip_not_enabled",
            "forbidden (403): GPT-Live SIP may not be enabled for this OpenAI project, or the key "
            "lacks access",
            "Erişim reddedildi (403): bu OpenAI projesinde GPT-Live SIP etkin olmayabilir; "
            "OpenAI desteğinden etkinleştirmelerini isteyin.",
        )
    return AcceptFailure(
        "other",
        f"accept failed (HTTP {status}, code {code})",
        f"OpenAI accept isteği başarısız (HTTP {status}).",
    )
