"""Owner commands in Telegram: /calls, /messages, /mode, /rules, /status (replies in Turkish)."""

import logging
from collections.abc import Callable
from zoneinfo import ZoneInfo

from app import db
from app.answer_mode import ALIASES, AnswerMode, AnswerModeStore
from app.rules import ContactAction, RulesConfig, normalize_number

logger = logging.getLogger(__name__)

# Telegram rejects messages above 4096 characters.
MAX_REPLY_CHARS = 3900
RECENT_CALLS = 10
LOCAL_TZ = ZoneInfo("Europe/Vilnius")

HELP_TEXT = (
    "Komutlar:\n"
    "/calls - son 10 arama ve özetleri\n"
    "/messages - okunmamış arayan mesajları\n"
    "/mode - cevaplama modunu değiştir (herkes / kayıtlı numaralar / kapalı)\n"
    "/rules - yüklü kurallar\n"
    "/status - durum"
)

_ACTIONS_TR = {
    ContactAction.NOTIFY_IMMEDIATELY: "hemen sana sor",
    ContactAction.SCREEN: "bilgi topla, sonra sor",
    ContactAction.POLITE_REJECT: "kibarca reddet",
}


def mask_number(number: str | None) -> str:
    """Last three digits only: enough to recognize, nothing to leak."""
    digits = normalize_number(number or "")
    return f"…{digits[-3:]}" if len(digits) >= 3 else "gizli"


def clip(text: str, limit: int = MAX_REPLY_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_rules(config: RulesConfig, *, off_mode_action: str) -> str:
    lines = ["📋 Yüklü kurallar"]
    allow = config.do_not_answer.allow
    deny = config.do_not_answer.deny
    if allow:
        lines.append("\n✅ İzinli (her modda geçer, çalınca haber verilir):")
        for c in allow:
            extra = " · yönlendirme teklif edilir" if c.offer_forward else ""
            lines.append(f"• {c.name} {mask_number(c.number)}: “{c.alert_text}”{extra}")
    if deny:
        lines.append("\n⛔ Engelli (cevaplanmaz, meşgul sesi):")
        lines += [f"• {mask_number(b.number)} {b.note}".rstrip() for b in deny]
    if config.known_numbers:
        lines.append("\n👥 Kayıtlı numaralar:")
        for c in config.known_numbers:
            lines.append(
                f"• {c.name} ({c.relation}) {mask_number(c.number)}: {_ACTIONS_TR[c.action]}"
            )
    if config.keyword_rules:
        lines.append("\n🏷 Arayan türleri:")
        for rule in config.keyword_rules:
            lines.append(f"• {rule.name}: {_ACTIONS_TR[rule.action]}")
    lines.append(f"\n❔ Bilinmeyen arayan: {config.unknown_caller_guidance.strip()}")
    off = "sesli mesaj alınır" if off_mode_action == "voicemail" else "reddedilir"
    lines.append(f"🔴 Kapalı modda arayanlar: {off}")
    return "\n".join(lines)


class OwnerCommands:
    def __init__(
        self,
        *,
        db_path: str,
        modes: AnswerModeStore,
        rules: RulesConfig,
        off_mode_action: str,
        status_lines: Callable[[], list[str]],
    ) -> None:
        self._db_path = db_path
        self._modes = modes
        self._rules = rules
        self._off_mode_action = off_mode_action
        self._status_lines = status_lines

    async def handle(self, text: str) -> str:
        command, _, argument = text.strip().partition(" ")
        # "/calls@my_bot" in group-style clients.
        command = command.split("@", 1)[0].casefold()
        handlers = {
            "/calls": self.calls,
            "/messages": self.messages,
            "/rules": self.rules,
            "/status": self.status,
        }
        try:
            if command == "/mode":
                return await self.mode(argument.strip())
            handler = handlers.get(command)
            return await handler() if handler else HELP_TEXT
        except Exception:
            logger.exception("Owner command %s failed", command)
            return "⚠️ Komut çalışmadı (veritabanı hatası). Kayıtlara bakın."

    async def calls(self) -> str:
        rows = await db.recent_calls(self._db_path, RECENT_CALLS)
        if not rows:
            return "Henüz arama yok."
        parts = [f"📞 Son {len(rows)} arama"]
        for row in rows:
            when = row.started_at.astimezone(LOCAL_TZ).strftime("%d.%m %H:%M")
            icon = "🚩" if row.flagged else ("📌" if row.follow_up else "•")
            body = row.summary or (
                f"(özet yok) {row.end_reason}" if row.end_reason else "(özet yok)"
            )
            parts.append(f"{icon} {when} · {row.caller_number}\n{clip(body, 320)}")
        return clip("\n\n".join(parts))

    async def messages(self) -> str:
        rows = await db.undelivered_messages(self._db_path)
        if not rows:
            return "📭 Okunmamış mesaj yok."
        parts = [f"📬 Okunmamış {len(rows)} mesaj"]
        shown: list[int] = []
        for row in rows:
            when = row.created_at.astimezone(LOCAL_TZ).strftime("%d.%m %H:%M")
            entry = f"📝 {when} · {row.caller_number or 'bilinmiyor'}\n{clip(row.message, 600)}"
            if sum(len(p) + 2 for p in parts) + len(entry) > MAX_REPLY_CHARS:
                parts.append("… devamı için /messages komutunu tekrar gönderin.")
                break
            parts.append(entry)
            shown.append(row.id)
        await db.mark_messages_delivered(self._db_path, shown)
        return "\n\n".join(parts)

    async def mode(self, argument: str) -> str:
        if argument:
            wanted = ALIASES.get(argument.casefold())
            if wanted is None:
                try:
                    wanted = AnswerMode(argument.casefold())
                except ValueError:
                    return "Bilinmeyen mod. Kullanım: /mode, /mode all, /mode known, /mode off"
            mode = await self._modes.set(wanted)
        else:
            mode = await self._modes.cycle()
        return f"Mod: {mode.label_tr}\n(/mode ile sıradakine geçin)"

    async def rules(self) -> str:
        return clip(format_rules(self._rules, off_mode_action=self._off_mode_action))

    async def status(self) -> str:
        undelivered = await db.count_undelivered_messages(self._db_path)
        lines = ["🤖 Jarvis çalışıyor", f"Mod: {self._modes.mode.label_tr}"]
        lines += self._status_lines()
        lines.append(f"Okunmamış mesaj: {undelivered}")
        return "\n".join(lines)
