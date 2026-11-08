"""Comandos personalizados del chat (``!nombre``) y comandos de gestión para mods."""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Awaitable, Callable

from ..core.event_bus import EventBus
from ..core.models import (
    ALL_PLATFORMS,
    ChatMessage,
    Permission,
    Platform,
    platforms_from_str,
    platforms_to_str,
)
from ..platforms.base import PlatformError
from ..storage.database import Database, utc_now_iso
from .variables import Fetcher, RenderContext, render

if TYPE_CHECKING:
    from ..bot import AlpekaBot

log = logging.getLogger(__name__)

PREFIX = "!"
MAX_REPLY_LEN = 500
_NAME_RE = re.compile(r"^[\wáéíóúüñ]{1,32}$", re.IGNORECASE)

# Nombres de comandos internos (gestión, canciones...): no se pueden usar para comandos personalizados.
# Se rellena al registrar handlers en CommandService.register().
RESERVED_NAMES: set[str] = {"addcmd", "editcmd", "delcmd"}

# Handler de un comando interno: (mensaje, argumentos) -> respuesta o None
BuiltinHandler = Callable[[ChatMessage, list[str]], Awaitable[str | None]]


@dataclass(frozen=True)
class Command:
    name: str
    response: str
    permission: Permission = Permission.EVERYONE
    cooldown_s: int = 5
    platforms: frozenset[Platform] = ALL_PLATFORMS
    enabled: bool = True
    use_count: int = 0
    id: int | None = None


def is_valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name))


def normalize_name(raw: str) -> str:
    """'!Hola' -> 'hola'. Lanza ValueError con un mensaje para el usuario."""
    name = raw.strip().lstrip(PREFIX).lower()
    if not _NAME_RE.match(name):
        raise ValueError("El nombre solo puede tener letras, números y _ (máx. 32).")
    if name in RESERVED_NAMES:
        raise ValueError(f"!{name} es un comando interno y no se puede usar.")
    return name


# ------------------------------------------------------------- repositorio
class CommandRepository:
    def __init__(self, db: Database, bus: EventBus) -> None:
        self._db = db
        self._bus = bus

    @staticmethod
    def _from_row(r) -> Command:
        return Command(
            id=r["id"],
            name=r["name"],
            response=r["response"],
            permission=Permission(r["permission"]),
            cooldown_s=r["cooldown_s"],
            platforms=platforms_from_str(r["platforms"]),
            enabled=bool(r["enabled"]),
            use_count=r["use_count"],
        )

    def list_all(self) -> list[Command]:
        rows = self._db.conn.execute("SELECT * FROM commands ORDER BY name").fetchall()
        return [self._from_row(r) for r in rows]

    def get(self, name: str) -> Command | None:
        row = self._db.conn.execute("SELECT * FROM commands WHERE name = ?", (name,)).fetchone()
        return self._from_row(row) if row else None

    def add(self, cmd: Command) -> Command:
        name = normalize_name(cmd.name)
        self._validate(cmd)
        now = utc_now_iso()
        try:
            with self._db.conn:
                cur = self._db.conn.execute(
                    "INSERT INTO commands (name, response, permission, cooldown_s, platforms, enabled,"
                    " use_count, created_at, updated_at) VALUES (?,?,?,?,?,?,0,?,?)",
                    (name, cmd.response.strip(), int(cmd.permission), cmd.cooldown_s,
                     platforms_to_str(cmd.platforms), int(cmd.enabled), now, now),
                )
        except sqlite3.IntegrityError:
            raise ValueError(f"Ya existe el comando !{name}.") from None
        self._bus.commands_changed.emit()
        return replace(cmd, id=cur.lastrowid, name=name, use_count=0)

    def update(self, cmd: Command) -> None:
        if cmd.id is None:
            raise ValueError("Comando sin id")
        name = normalize_name(cmd.name)
        self._validate(cmd)
        try:
            with self._db.conn:
                self._db.conn.execute(
                    "UPDATE commands SET name=?, response=?, permission=?, cooldown_s=?, platforms=?,"
                    " enabled=?, updated_at=? WHERE id=?",
                    (name, cmd.response.strip(), int(cmd.permission), cmd.cooldown_s,
                     platforms_to_str(cmd.platforms), int(cmd.enabled), utc_now_iso(), cmd.id),
                )
        except sqlite3.IntegrityError:
            raise ValueError(f"Ya existe el comando !{name}.") from None
        self._bus.commands_changed.emit()

    def delete(self, cmd_id: int) -> None:
        with self._db.conn:
            self._db.conn.execute("DELETE FROM commands WHERE id = ?", (cmd_id,))
        self._bus.commands_changed.emit()

    def increment_use(self, cmd_id: int) -> int:
        with self._db.conn:
            self._db.conn.execute("UPDATE commands SET use_count = use_count + 1 WHERE id = ?", (cmd_id,))
        count = self._db.conn.execute("SELECT use_count FROM commands WHERE id = ?", (cmd_id,)).fetchone()[0]
        self._bus.commands_changed.emit()
        return count

    @staticmethod
    def _validate(cmd: Command) -> None:
        if not cmd.response.strip():
            raise ValueError("La respuesta no puede estar vacía.")
        if not 0 <= cmd.cooldown_s <= 86_400:
            raise ValueError("El cooldown debe estar entre 0 y 86400 segundos.")
        if not cmd.platforms:
            raise ValueError("Elige al menos una plataforma.")


# ----------------------------------------------------------------- servicio
class CommandService:
    """Escucha el chat, ejecuta comandos y responde."""

    def __init__(
        self,
        bot: "AlpekaBot",
        repo: CommandRepository,
        clock: Callable[[], float] = time.monotonic,
        fetch: Fetcher | None = None,
    ) -> None:
        self._bot = bot
        self._repo = repo
        self._clock = clock
        self._fetch = fetch
        self._last_used: dict[str, float] = {}
        self._handlers: dict[str, tuple[Permission, BuiltinHandler]] = {}
        self.register("addcmd", self._cmd_add, Permission.MODERATOR)
        self.register("editcmd", self._cmd_edit, Permission.MODERATOR)
        self.register("delcmd", self._cmd_del, Permission.MODERATOR)
        bot.bus.chat_message.connect(self._on_message)

    def register(self, name: str, handler: BuiltinHandler, permission: Permission = Permission.EVERYONE) -> None:
        """Registra un comando interno. Tiene prioridad sobre los personalizados."""
        name = name.lower()
        RESERVED_NAMES.add(name)
        self._handlers[name] = (permission, handler)

    def unregister(self, name: str) -> None:
        """Quita un comando interno (p. ej. al renombrar el del TTS): el nombre queda libre."""
        name = name.lower()
        RESERVED_NAMES.discard(name)
        self._handlers.pop(name, None)

    def is_taken(self, name: str) -> bool:
        """¿Ya hay un comando interno o personalizado con ese nombre?"""
        name = name.lower()
        return name in self._handlers or self._repo.get(name) is not None

    def is_builtin(self, name: str) -> bool:
        return name.lower() in self._handlers

    def builtins(self) -> list[tuple[str, Permission]]:
        """Comandos internos registrados (nombre, permiso mínimo), para mostrarlos en la app."""
        return [(name, perm) for name, (perm, _h) in self._handlers.items()]

    def _on_message(self, msg: ChatMessage) -> None:
        moderation = getattr(self._bot, "moderation", None)
        if moderation is not None and moderation.is_blocked(msg):
            return  # el mensaje rompió un filtro: no se ejecuta
        if msg.text.startswith(PREFIX):
            asyncio.ensure_future(self._safe_handle(msg))

    async def _safe_handle(self, msg: ChatMessage) -> None:
        try:
            await self.handle(msg)
        except Exception:
            log.exception("Error ejecutando comando %r", msg.text[:50])

    async def handle(self, msg: ChatMessage) -> str | None:
        """Procesa un mensaje. Devuelve la respuesta enviada (o None)."""
        client = self._bot.platforms.get(msg.platform)
        if client is None or msg.user_id == client.bot_user_id:
            return None  # nunca responder a los mensajes del propio bot
        parts = msg.text[len(PREFIX):].split()
        if not parts:
            return None
        name = parts[0].lower()

        builtin = self._handlers.get(name)
        if builtin is not None:
            permission, handler = builtin
            reply = await handler(msg, parts[1:]) if msg.permission >= permission else None
        else:
            reply = await self._custom(name, parts[1:], msg)
        if reply:
            reply = reply[:MAX_REPLY_LEN]
            try:
                await self._bot.send_message(msg.platform, reply)
            except PlatformError as exc:
                log.warning("No se pudo responder a !%s: %s", name, exc)
        return reply

    async def _custom(self, name: str, args: list[str], msg: ChatMessage) -> str | None:
        cmd = self._repo.get(name)
        if cmd is None or not cmd.enabled or msg.platform not in cmd.platforms:
            return None
        if msg.permission < cmd.permission:
            return None
        now = self._clock()
        last = self._last_used.get(cmd.name)
        if last is not None and now - last < cmd.cooldown_s:
            return None
        self._last_used[cmd.name] = now
        count = self._repo.increment_use(cmd.id)
        client = self._bot.platforms[msg.platform]
        ctx = RenderContext(
            platform=msg.platform,
            channel=client.channel_name,
            user_name=msg.user_name,
            args=args,
            count=count,
            stream=client.stream_info,
        )
        return await render(cmd.response, ctx, self._fetch)

    # ---------------------------------------------- !addcmd !editcmd !delcmd
    async def _cmd_add(self, msg: ChatMessage, _args: list[str]) -> str | None:
        return self._manage("addcmd", msg)

    async def _cmd_edit(self, msg: ChatMessage, _args: list[str]) -> str | None:
        return self._manage("editcmd", msg)

    async def _cmd_del(self, msg: ChatMessage, _args: list[str]) -> str | None:
        return self._manage("delcmd", msg)

    def _manage(self, name: str, msg: ChatMessage) -> str | None:
        at = f"@{msg.user_name}"
        parts = msg.text.split(maxsplit=2)  # ["!addcmd", "!nombre", "respuesta..."]
        try:
            if name == "delcmd":
                if len(parts) < 2:
                    return f"{at} Uso: !delcmd !nombre"
                cmd_name = normalize_name(parts[1])
                cmd = self._repo.get(cmd_name)
                if cmd is None:
                    return f"{at} No existe el comando !{cmd_name}."
                self._repo.delete(cmd.id)
                return f"{at} Comando !{cmd_name} eliminado."

            if len(parts) < 3:
                return f"{at} Uso: !{name} !nombre respuesta"
            cmd_name, response = normalize_name(parts[1]), parts[2]
            if name == "addcmd":
                self._repo.add(Command(name=cmd_name, response=response))
                return f"{at} Comando !{cmd_name} creado."
            cmd = self._repo.get(cmd_name)
            if cmd is None:
                return f"{at} No existe el comando !{cmd_name}."
            self._repo.update(replace(cmd, response=response))
            return f"{at} Comando !{cmd_name} actualizado."
        except ValueError as exc:
            return f"{at} {exc}"
