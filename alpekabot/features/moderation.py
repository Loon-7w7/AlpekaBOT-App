"""Moderación unificada Twitch + Kick: filtros automáticos, strikes y acciones manuales.

- Filtros: enlaces (con lista blanca y ``!permit``) y palabras prohibidas.
- Escalado con strikes comunes a todos los filtros, por usuario y plataforma.
- Comandos para mods: ``!timeout !ban !unban !permit``.
- Historial de acciones en SQLite (tabla ``mod_actions``).

Todo trabaja con ``ChatMessage``; cada plataforma ejecuta las acciones con su API.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
import unicodedata
from collections import OrderedDict, deque
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Callable

from ..config import ModerationConfig
from ..core.event_bus import EventBus
from ..core.models import ChatMessage, Permission, Platform
from ..platforms.base import PlatformError
from ..storage.database import Database, utc_now_iso

if TYPE_CHECKING:
    from ..bot import AlpekaBot
    from .commands import CommandService

log = logging.getLogger(__name__)

BOT_NAME = "AlpekaBOT"
DEFAULT_TIMEOUT_S = 600
BAN_STEP = -1  # valor de un paso del escalado que significa "ban permanente"

REASON_LINK = "no se permiten enlaces"
REASON_WORD = "lenguaje no permitido"

ACTION_LABELS = {
    "warn": "Aviso (borrado)",
    "delete": "Borrado",
    "timeout": "Timeout",
    "ban": "Ban",
    "unban": "Desbaneo",
    "permit": "Permiso de enlace",
}


# ======================================================== utilidades de texto
def format_duration(seconds: int) -> str:
    """600 -> '10 min', 5400 -> '1 h 30 min'."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    days, rem = divmod(seconds, 86_400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days} d")
    if hours:
        parts.append(f"{hours} h")
    if minutes:
        parts.append(f"{minutes} min")
    return " ".join(parts) or "0 s"


_DURATION_RE = re.compile(r"^(\d{1,7})\s*(s|seg|m|min|h|d)?$", re.IGNORECASE)
_UNITS = {"s": 1, "seg": 1, "m": 60, "min": 60, "h": 3600, "d": 86_400}


def parse_duration(text: str) -> int | None:
    """'600' / '30s' / '10m' / '1h' / '2d' -> segundos. None si no es una duración."""
    m = _DURATION_RE.match(text.strip())
    if not m:
        return None
    value = int(m.group(1)) * _UNITS[(m.group(2) or "s").lower()]
    return value if value > 0 else None


def format_escalation(steps: list[int]) -> str:
    """[0, 60, -1] -> 'aviso, 60, ban'."""
    return ", ".join("aviso" if s == 0 else "ban" if s < 0 else str(s) for s in steps)


def parse_escalation(text: str) -> list[int]:
    """'aviso, 1m, 600, ban' -> [0, 60, 600, -1]. Lanza ValueError con un mensaje para el usuario."""
    steps = []
    for raw in text.split(","):
        token = raw.strip().lower()
        if not token:
            continue
        if token == "aviso":
            steps.append(0)
        elif token == "ban":
            steps.append(BAN_STEP)
        else:
            seconds = parse_duration(token)
            if seconds is None:
                raise ValueError(f"«{raw.strip()}» no es válido: usa aviso, ban o una duración (60, 10m, 1h).")
            steps.append(seconds)
    if not steps:
        raise ValueError("El escalado necesita al menos un paso.")
    if len(steps) > 10:
        raise ValueError("Como máximo 10 pasos.")
    return steps


def fold(text: str) -> str:
    """Minúsculas y sin acentos: 'Pútá' -> 'puta'. (La ñ también pierde la tilde.)"""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


# --------------------------------------------------------------- enlaces
_TLDS = (
    "com|net|org|info|biz|io|tv|gg|me|co|xyz|app|dev|ly|be|es|mx|ar|cl|pe|uy|ve|ec|bo|py|"
    "us|uk|ca|eu|de|fr|it|pt|br|ru|cn|jp|in|nl|pl|se|ch|at|au|nz|"
    "link|site|online|shop|store|live|club|top|cc|to|sh|ai|gl|tk|ml|ga|cf|gq|ws|pw|fun|icu|"
    "vip|win|bet|cam|lol|click|zip|mov|art|blog|news|page|cloud|tech|world|today|pro|so|im|fm|am|"
    "ink|one|lat|gay|sex|xxx|porn|tube|stream|games|chat|social|space|website|email"
)
# Enlaces con esquema o www., o dominios sueltos con un TLD conocido ("pagina.com/x").
# No cuenta los correos (lo que va detrás de @).
_LINK_RE = re.compile(
    r"(?:https?://|www\.)[^\s<>\"']+"
    rf"|(?<![\w@.\-])(?:[a-z0-9](?:[a-z0-9\-]{{0,61}}[a-z0-9])?\.)+(?:{_TLDS})(?![\w\-])(?:[:/?#][^\s<>\"']*)?",
    re.IGNORECASE,
)


def _host_of(link: str) -> str:
    s = re.sub(r"^https?://", "", link, flags=re.IGNORECASE)
    authority = re.split(r"[/?#]", s, maxsplit=1)[0]
    authority = authority.rsplit("@", 1)[-1]  # usuario:clave@host
    return authority.split(":", 1)[0].lower().rstrip(".")


def find_links(text: str) -> list[str]:
    """Dominios de los enlaces que aparecen en el texto."""
    return [h for h in (_host_of(m.group(0)) for m in _LINK_RE.finditer(text)) if h]


def remove_links(text: str) -> str:
    """El texto sin sus enlaces (para que el TTS no los lea letra por letra)."""
    return _LINK_RE.sub(" ", text)


def normalize_domain(raw: str) -> str:
    return _host_of(raw.strip()).removeprefix("www.")


def is_whitelisted(host: str, whitelist: list[str]) -> bool:
    host = host.removeprefix("www.")
    for d in whitelist:
        d = normalize_domain(d)
        if d and (host == d or host.endswith("." + d)):
            return True
    return False


def forbidden_link(text: str, whitelist: list[str]) -> str | None:
    """Primer dominio NO permitido del mensaje, o None."""
    for host in find_links(text):
        if not is_whitelisted(host, whitelist):
            return host
    return None


# ------------------------------------------------------ palabras prohibidas
# Caracteres que suelen usarse para sustituir cada letra ("pvt4" -> "puta")
_LOOKALIKES = {
    "a": "a4@", "b": "b8", "e": "e3€", "g": "g9", "i": "i1!|l", "l": "l1|",
    "o": "o0", "s": "s5$z", "t": "t7+", "u": "uv", "z": "z2",
}
_SEP = r"[\W_]{0,3}"  # separadores entre letras: "p.u.t.a", "p u t a"


def compile_word(pattern: str, is_regex: bool = False) -> re.Pattern:
    """Compila una entrada de la lista. Lanza ValueError si no es válida.

    Palabra normal: coincide con la palabra completa (también en plural), sin
    distinguir mayúsculas ni acentos, con letras repetidas ("puuuta"),
    separadores ("p.u.t.a") y números o símbolos en lugar de letras ("pvt4").
    Regex: se aplica tal cual sobre el texto en minúsculas y sin acentos.
    """
    if is_regex:
        try:
            return re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"Expresión regular no válida: {exc}") from None
    letters = [c for c in fold(pattern) if c.isalnum()]
    if not letters:
        raise ValueError("La palabra debe tener al menos una letra o número.")
    body = _SEP.join(f"[{re.escape(_LOOKALIKES.get(c, c))}]+" for c in letters)
    return re.compile(rf"(?<![^\W_]){body}(?:e?s)?(?![^\W_])")


# ============================================================= repositorio
@dataclass(frozen=True)
class BannedWord:
    id: int
    pattern: str
    is_regex: bool


@dataclass(frozen=True)
class ModAction:
    platform: Platform
    user_id: str
    user_name: str
    action: str            # warn, delete, timeout, ban, unban, permit
    duration_s: int = 0
    reason: str = ""
    source: str = "auto"   # auto | manual
    moderator: str = ""
    message: str = ""
    created_at: str = ""
    id: int | None = None

    @property
    def label(self) -> str:
        text = ACTION_LABELS.get(self.action, self.action)
        if self.action in ("timeout", "permit") and self.duration_s:
            text += f" {format_duration(self.duration_s)}"
        return text


class ModerationRepository:
    def __init__(self, db: Database, bus: EventBus) -> None:
        self._db = db
        self._bus = bus
        self._compiled: list[re.Pattern] | None = None

    # ---------------------------------------------------------- palabras
    def words_list(self) -> list[BannedWord]:
        rows = self._db.conn.execute("SELECT * FROM banned_words ORDER BY is_regex, pattern").fetchall()
        return [BannedWord(r["id"], r["pattern"], bool(r["is_regex"])) for r in rows]

    def word_add(self, pattern: str, is_regex: bool = False) -> None:
        pattern = pattern.strip()
        if not pattern:
            raise ValueError("Escribe una palabra.")
        if len(pattern) > 200:
            raise ValueError("Máximo 200 caracteres.")
        compile_word(pattern, is_regex)  # valida
        stored = pattern if is_regex else fold(pattern)
        try:
            with self._db.conn:
                self._db.conn.execute(
                    "INSERT INTO banned_words (pattern, is_regex, created_at) VALUES (?,?,?)",
                    (stored, int(is_regex), utc_now_iso()),
                )
        except sqlite3.IntegrityError:
            raise ValueError("Esa entrada ya está en la lista.") from None
        self._words_changed()

    def word_remove(self, word_id: int) -> None:
        with self._db.conn:
            self._db.conn.execute("DELETE FROM banned_words WHERE id = ?", (word_id,))
        self._words_changed()

    def _words_changed(self) -> None:
        self._compiled = None
        self._bus.banned_words_changed.emit()

    def compiled_words(self) -> list[re.Pattern]:
        if self._compiled is None:
            compiled = []
            for w in self.words_list():
                try:
                    compiled.append(compile_word(w.pattern, w.is_regex))
                except ValueError:
                    log.warning("Palabra prohibida no válida ignorada: %r", w.pattern)
            self._compiled = compiled
        return self._compiled

    # --------------------------------------------------------- historial
    def log_action(self, a: ModAction) -> ModAction:
        created = a.created_at or utc_now_iso()
        with self._db.conn:
            cur = self._db.conn.execute(
                "INSERT INTO mod_actions (created_at, platform, user_id, user_name, action, duration_s,"
                " reason, source, moderator, message) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (created, a.platform.value, a.user_id, a.user_name, a.action, int(a.duration_s),
                 a.reason[:200], a.source, a.moderator, a.message[:500]),
            )
        self._bus.mod_actions_changed.emit()
        return replace(a, id=cur.lastrowid, created_at=created)

    def recent_actions(self, limit: int = 500) -> list[ModAction]:
        rows = self._db.conn.execute(
            "SELECT * FROM mod_actions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            ModAction(
                platform=Platform(r["platform"]), user_id=r["user_id"], user_name=r["user_name"],
                action=r["action"], duration_s=r["duration_s"], reason=r["reason"], source=r["source"],
                moderator=r["moderator"], message=r["message"], created_at=r["created_at"], id=r["id"],
            )
            for r in rows
        ]

    def clear_actions(self) -> None:
        with self._db.conn:
            self._db.conn.execute("DELETE FROM mod_actions")
        self._bus.mod_actions_changed.emit()


# ================================================================ servicio
@dataclass(frozen=True)
class Chatter:
    user_id: str
    user_name: str
    badges: tuple[str, ...]


class ModerationService:
    """Revisa cada mensaje, sanciona según el escalado y ofrece las acciones manuales."""

    MAX_CHATTERS = 5000
    MAX_BLOCKED = 500

    def __init__(
        self,
        bot: "AlpekaBot",
        repo: ModerationRepository,
        config: ModerationConfig,
        save_config: Callable[[], None] = lambda: None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bot = bot
        self.repo = repo
        self.config = config
        self._save_config = save_config
        self._clock = clock
        self._chatters: OrderedDict[tuple[Platform, str], Chatter] = OrderedDict()
        self._strikes: dict[tuple[Platform, str], tuple[int, float]] = {}
        self._permits: dict[tuple[Platform, str], float] = {}
        self._blocked: deque[tuple[Platform, str]] = deque(maxlen=self.MAX_BLOCKED)
        self._tasks: set[asyncio.Task] = set()
        # Debe conectarse ANTES que CommandService: así un mensaje sancionado no ejecuta comandos
        bot.bus.chat_message.connect(self._on_message)

    # ------------------------------------------------------------ ajustes
    def save(self) -> None:
        self._save_config()

    @property
    def steps(self) -> list[int]:
        steps = [int(s) for s in self.config.escalation if isinstance(s, (int, float))]
        return steps or [0]

    # ------------------------------------------------------- chat entrante
    def is_blocked(self, msg: ChatMessage) -> bool:
        """True si el mensaje rompió un filtro (CommandService no lo ejecuta)."""
        return (msg.platform, msg.message_id) in self._blocked

    def _on_message(self, msg: ChatMessage) -> None:
        client = self._bot.platforms.get(msg.platform)
        if client is None or msg.user_id == client.bot_user_id:
            return
        self._remember(msg)
        reason = self.check(msg)
        if reason:
            self._blocked.append((msg.platform, msg.message_id))
            count, step = self._add_strike(msg.platform, msg.user_id)
            self._spawn(self._punish(msg, reason, count, step))

    def _remember(self, msg: ChatMessage) -> None:
        key = (msg.platform, msg.user_login)
        self._chatters[key] = Chatter(msg.user_id, msg.user_name, msg.badges)
        self._chatters.move_to_end(key)
        while len(self._chatters) > self.MAX_CHATTERS:
            self._chatters.popitem(last=False)

    def check(self, msg: ChatMessage) -> str | None:
        """Motivo de la infracción, o None si el mensaje está permitido."""
        cfg = self.config
        level = msg.permission
        if not cfg.enabled or level >= Permission.MODERATOR:
            return None  # mods y streamer siempre exentos
        if cfg.words_enabled and level < cfg.words_exempt:
            folded = fold(msg.text)
            if any(p.search(folded) for p in self.repo.compiled_words()):
                return REASON_WORD
        if cfg.links_enabled and level < cfg.links_exempt:
            if forbidden_link(msg.text, cfg.links_whitelist) and not self._use_permit(msg.platform, msg.user_login):
                return REASON_LINK
        return None

    # ---------------------------------------------------------- strikes
    def _add_strike(self, platform: Platform, user_id: str) -> tuple[int, int]:
        """Suma una infracción. Devuelve (número de infracción, paso del escalado)."""
        now = self._clock()
        key = (platform, user_id)
        count, last = self._strikes.get(key, (0, now))
        if now - last > self.config.strike_reset_min * 60:
            count = 0
        count += 1
        self._strikes[key] = (count, now)
        steps = self.steps
        return count, steps[min(count, len(steps)) - 1]

    def strikes_of(self, platform: Platform, user_id: str) -> int:
        entry = self._strikes.get((platform, user_id))
        if entry is None or self._clock() - entry[1] > self.config.strike_reset_min * 60:
            return 0
        return entry[0]

    async def _punish(self, msg: ChatMessage, reason: str, count: int, step: int) -> None:
        client = self._bot.platforms[msg.platform]
        deleted = sanctioned = False
        try:
            await client.delete_message(msg.message_id)
            deleted = True
        except PlatformError as exc:
            log.warning("Moderación: %s", exc)
        action, duration = "warn", 0
        if step != 0:
            try:
                if step > 0:
                    await client.timeout_user(msg.user_id, step, f"{BOT_NAME}: {reason}")
                    action, duration = "timeout", step
                else:
                    await client.ban_user(msg.user_id, f"{BOT_NAME}: {reason}")
                    action = "ban"
                sanctioned = True
            except PlatformError as exc:
                log.warning("Moderación: %s", exc)
        if not (deleted or sanctioned):
            return  # no se pudo hacer nada (el aviso ya está en el log)
        self._record(ModAction(
            msg.platform, msg.user_id, msg.user_name, action, duration, reason,
            source="auto", moderator=BOT_NAME, message=msg.text,
        ))
        if self.config.announce:
            if action == "timeout":
                text = f"@{msg.user_name} {reason}: timeout de {format_duration(duration)}."
            elif action == "ban":
                text = f"@{msg.user_name} {reason}: baneado."
            else:
                text = f"@{msg.user_name} {reason} (aviso {count})."
            await self._say(msg.platform, text)

    def _record(self, action: ModAction) -> ModAction:
        saved = self.repo.log_action(action)
        log.info("Moderación %s: %s → %s%s", action.platform.label, action.user_name, action.label,
                 f" ({action.reason})" if action.reason else "")
        self._bot.bus.mod_action.emit(saved)
        return saved

    async def _say(self, platform: Platform, text: str) -> None:
        try:
            await self._bot.send_message(platform, text)
        except PlatformError as exc:
            log.warning("Moderación: no se pudo avisar en el chat (%s)", exc)

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("Error en la moderación", exc_info=task.exception())

    async def wait_idle(self) -> None:
        """Espera a que terminen las sanciones en curso (tests y cierre)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # ----------------------------------------------------------- !permit
    def permit(self, platform: Platform, login: str, user_name: str = "", moderator: str = "App") -> int:
        """Deja a un usuario enviar UN enlace durante ``permit_s`` segundos. Devuelve los segundos."""
        login = login.strip().lstrip("@").lower()
        seconds = max(5, int(self.config.permit_s))
        self._permits[(platform, login)] = self._clock() + seconds
        chatter = self._chatters.get((platform, login))
        self._record(ModAction(
            platform, chatter.user_id if chatter else "", user_name or (chatter.user_name if chatter else login),
            "permit", seconds, source="manual", moderator=moderator,
        ))
        return seconds

    def _use_permit(self, platform: Platform, login: str) -> bool:
        expires = self._permits.pop((platform, login), None)
        return expires is not None and self._clock() <= expires

    # -------------------------------------------------- acciones manuales
    def known_chatter(self, platform: Platform, login: str) -> Chatter | None:
        return self._chatters.get((platform, login.strip().lstrip("@").lower()))

    async def resolve(self, platform: Platform, name: str) -> Chatter:
        """Busca a un usuario por su nombre (primero entre los que han escrito). Lanza PlatformError."""
        login = name.strip().lstrip("@").lower()
        if not re.fullmatch(r"[\w\-]{1,40}", login):
            raise PlatformError("Nombre de usuario no válido.")
        known = self._chatters.get((platform, login))
        if known:
            return known
        client = self._bot.platforms.get(platform)
        found = await client.lookup_user(login) if client else None
        if not found:
            raise PlatformError(f"No encuentro a «{login}» en {platform.label}.")
        return Chatter(found[0], found[1], ())

    def _check_target(self, platform: Platform, target: Chatter) -> None:
        client = self._bot.platforms.get(platform)
        if client is not None and target.user_id == client.bot_user_id:
            raise PlatformError("No puedo sancionarme a mí mismo.")
        if "broadcaster" in target.badges or "moderator" in target.badges:
            raise PlatformError(f"{target.user_name} es moderador o el streamer: no se le puede sancionar.")

    async def delete(self, msg: ChatMessage, moderator: str = "App") -> None:
        await self._bot.platforms[msg.platform].delete_message(msg.message_id)
        self._record(ModAction(msg.platform, msg.user_id, msg.user_name, "delete",
                               source="manual", moderator=moderator, message=msg.text))

    async def timeout(self, platform: Platform, target: Chatter, seconds: int,
                      reason: str = "", moderator: str = "App") -> None:
        self._check_target(platform, target)
        await self._bot.platforms[platform].timeout_user(target.user_id, seconds, reason)
        self._record(ModAction(platform, target.user_id, target.user_name, "timeout", seconds, reason,
                               source="manual", moderator=moderator))

    async def ban(self, platform: Platform, target: Chatter, reason: str = "", moderator: str = "App") -> None:
        self._check_target(platform, target)
        await self._bot.platforms[platform].ban_user(target.user_id, reason)
        self._record(ModAction(platform, target.user_id, target.user_name, "ban", 0, reason,
                               source="manual", moderator=moderator))

    async def unban(self, platform: Platform, target: Chatter, moderator: str = "App") -> None:
        await self._bot.platforms[platform].unban_user(target.user_id)
        self._strikes.pop((platform, target.user_id), None)
        self._record(ModAction(platform, target.user_id, target.user_name, "unban",
                               source="manual", moderator=moderator))

    # -------------------------------------------------- comandos del chat
    def register_commands(self, commands: "CommandService") -> None:
        commands.register("timeout", self._cmd_timeout, Permission.MODERATOR)
        commands.register("ban", self._cmd_ban, Permission.MODERATOR)
        commands.register("unban", self._cmd_unban, Permission.MODERATOR)
        commands.register("permit", self._cmd_permit, Permission.MODERATOR)

    async def _cmd_timeout(self, msg: ChatMessage, args: list[str]) -> str | None:
        if not args:
            return f"@{msg.user_name} Uso: !timeout usuario [duración: 600, 10m, 1h] [motivo]"
        seconds = parse_duration(args[1]) if len(args) > 1 else None
        rest = args[2:] if seconds is not None else args[1:]
        seconds = seconds or DEFAULT_TIMEOUT_S
        return await self._manual(
            msg, args[0], lambda t, r: self.timeout(msg.platform, t, seconds, r, msg.user_name),
            " ".join(rest), f"timeout de {format_duration(seconds)}",
        )

    async def _cmd_ban(self, msg: ChatMessage, args: list[str]) -> str | None:
        if not args:
            return f"@{msg.user_name} Uso: !ban usuario [motivo]"
        return await self._manual(msg, args[0], lambda t, r: self.ban(msg.platform, t, r, msg.user_name),
                                  " ".join(args[1:]), "baneado")

    async def _cmd_unban(self, msg: ChatMessage, args: list[str]) -> str | None:
        if not args:
            return f"@{msg.user_name} Uso: !unban usuario"
        return await self._manual(msg, args[0], lambda t, _r: self.unban(msg.platform, t, msg.user_name),
                                  "", "desbaneado")

    async def _cmd_permit(self, msg: ChatMessage, args: list[str]) -> str | None:
        if not args:
            return f"@{msg.user_name} Uso: !permit usuario"
        login = args[0].lstrip("@")
        chatter = self.known_chatter(msg.platform, login)
        name = chatter.user_name if chatter else login
        seconds = self.permit(msg.platform, login, name, msg.user_name)
        return f"@{name} puedes enviar un enlace durante {format_duration(seconds)}."

    async def _manual(self, msg: ChatMessage, name: str, action, reason: str, done: str) -> str:
        try:
            target = await self.resolve(msg.platform, name)
            await action(target, reason)
        except PlatformError as exc:
            return f"@{msg.user_name} {exc}"[:500]
        return f"{target.user_name}: {done}."
