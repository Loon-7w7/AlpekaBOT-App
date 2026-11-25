"""Título y categoría del directo: comandos !title y !game y la página «Directo».

Reglas acordadas con el usuario:
- Ver el título o la categoría (sin texto): cualquiera. Cambiarlos: moderadores o el streamer.
- Sin prefijo se cambian en todas las plataformas conectadas; ``!title twitch …`` o
  ``!game kick …`` cambian solo esa.
- ``!game`` usa la coincidencia exacta si existe y si no la primera de la búsqueda,
  y dice en el chat cuál puso.
- Los comandos solo cambian las plataformas que están en directo (``live_only``); la
  página «Directo» no lo exige, para poder preparar el directo antes de empezar.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING

from ..core.models import Category, ChatMessage, Permission, Platform
from ..platforms.base import PlatformClient, PlatformError

if TYPE_CHECKING:
    from .commands import CommandService

log = logging.getLogger(__name__)

TITLE_COMMAND = "title"
GAME_COMMAND = "game"
CHANGE_PERMISSION = Permission.MODERATOR
SEARCH_LIMIT = 10
REFRESH_TIMEOUT_S = 5  # máximo para volver a consultar si una plataforma está en directo

# resultado por plataforma: None = bien, texto = motivo del fallo
Results = dict[Platform, str | None]


def best_category(query: str, found: list[Category]) -> Category | None:
    """La coincidencia exacta (sin mayúsculas) si existe; si no, la primera de la búsqueda."""
    q = query.strip().casefold()
    for c in found:
        if c.name.casefold() == q:
            return c
    return found[0] if found else None


def _names(platforms) -> str:
    return " y ".join(p.label for p in platforms)


class StreamControl:
    def __init__(self, platforms: Mapping[Platform, PlatformClient]) -> None:
        self._platforms = platforms

    # ------------------------------------------------------------ consulta
    def connected(self) -> list[Platform]:
        return [p for p, c in self._platforms.items() if c.is_connected]

    async def search(self, platform: Platform, query: str, limit: int = SEARCH_LIMIT) -> list[Category]:
        return await self._client(platform).search_categories(query, limit)

    def _client(self, platform: Platform) -> PlatformClient:
        client = self._platforms.get(platform)
        if client is None or not client.is_connected:
            raise PlatformError(f"{platform.label} no está conectado")
        return client

    # ------------------------------------------------------------- cambios
    async def set_title(self, title: str, platforms, live_only: bool = False) -> Results:
        title = title.strip()

        async def one(p: Platform) -> None:
            await self._client(p).update_stream(title=title)

        return await self._run(platforms, one, "el título", live_only)

    async def set_category(self, query: str, platforms,
                           live_only: bool = False) -> tuple[Results, dict[Platform, Category]]:
        """Busca la categoría en cada plataforma y la aplica. Devuelve (resultados, categoría puesta)."""
        applied: dict[Platform, Category] = {}

        async def one(p: Platform) -> None:
            client = self._client(p)
            cat = best_category(query, await client.search_categories(query, SEARCH_LIMIT))
            if cat is None:
                raise PlatformError(f"no encontré la categoría «{query.strip()}»")
            await client.update_stream(category=cat)
            applied[p] = cat

        return await self._run(platforms, one, "la categoría", live_only), applied

    async def _require_live(self, p: Platform) -> None:
        """Error si la plataforma no está en directo. Antes de rechazar vuelve a consultar,
        porque el estado guardado puede ir hasta un minuto atrasado."""
        client = self._client(p)
        if client.stream_info.is_live:
            return
        try:
            await asyncio.wait_for(client.refresh_stream(), REFRESH_TIMEOUT_S)
        except (PlatformError, asyncio.TimeoutError):
            raise PlatformError("no pude comprobar si está en directo") from None
        if not client.stream_info.is_live:
            raise PlatformError("no está en directo")

    async def _run(self, platforms, fn, what: str, live_only: bool = False) -> Results:
        """Aplica ``fn`` en cada plataforma a la vez. ``live_only``: solo en las que están en directo."""
        targets = list(dict.fromkeys(platforms))

        async def guarded(p: Platform) -> str | None:
            try:
                if live_only:
                    await self._require_live(p)
                await fn(p)
            except PlatformError as exc:
                log.warning("%s: no se pudo cambiar %s: %s", p.label, what, exc)
                return str(exc)
            except Exception:
                log.exception("%s: error cambiando %s", p.label, what)
                return "error inesperado (mira la consola)"
            log.info("%s: %s cambiado", p.label, what)
            return None

        errors = await asyncio.gather(*(guarded(p) for p in targets))
        return dict(zip(targets, errors))

    # ------------------------------------------------------------ comandos
    def register_commands(self, commands: "CommandService") -> None:
        # Permiso «Todos»: cualquiera puede VER; el permiso para cambiar se mira dentro
        commands.register(TITLE_COMMAND, self._cmd_title)
        commands.register(GAME_COMMAND, self._cmd_game)

    def _targets(self, msg: ChatMessage, args: list[str]) -> tuple[list[Platform], list[str], str | None]:
        """(plataformas, argumentos sin el prefijo, error). Sin prefijo = todas las conectadas."""
        if args:
            chosen = next((p for p in Platform if p.value == args[0].lower()), None)
            if chosen is not None:
                client = self._platforms.get(chosen)
                if client is None or not client.is_connected:
                    return [], args[1:], f"{chosen.label} no está conectado."
                return [chosen], args[1:], None
        return self.connected() or [msg.platform], args, None

    def _shown(self, targets: list[Platform], msg: ChatMessage) -> Platform:
        """Plataforma que se muestra al consultar: la del prefijo o donde se escribió."""
        return targets[0] if len(targets) == 1 else msg.platform

    async def _cmd_title(self, msg: ChatMessage, args: list[str]) -> str | None:
        targets, rest, error = self._targets(msg, args)
        if error:
            return error
        if not rest:
            p = self._shown(targets, msg)
            title = self._platforms[p].stream_info.title
            return f"Título en {p.label}: {title}" if title else f"{p.label} no tiene título."
        if msg.permission < CHANGE_PERMISSION:
            return None
        results = await self.set_title(" ".join(rest), targets, live_only=True)
        return self._summary(results, f"Título cambiado en {_names(p for p, e in results.items() if not e)}.")

    async def _cmd_game(self, msg: ChatMessage, args: list[str]) -> str | None:
        targets, rest, error = self._targets(msg, args)
        if error:
            return error
        if not rest:
            p = self._shown(targets, msg)
            game = self._platforms[p].stream_info.game
            return f"Categoría en {p.label}: {game}" if game else f"{p.label} no tiene categoría."
        if msg.permission < CHANGE_PERMISSION:
            return None
        results, applied = await self.set_category(" ".join(rest), targets, live_only=True)
        return self._summary(results, category_text(applied))

    @staticmethod
    def _summary(results: Results, ok_text: str) -> str:
        failed = [f"{p.label}: {e}" for p, e in results.items() if e]
        if len(failed) == len(results):
            return "No se pudo cambiar. " + " · ".join(failed)
        return ok_text + ("" if not failed else " No se pudo en " + " · ".join(failed))


def category_text(applied: dict[Platform, Category]) -> str:
    """«Categoría: Minecraft (Twitch y Kick).» o, si el nombre difiere, una por plataforma."""
    names = {c.name for c in applied.values()}
    if len(names) == 1:
        return f"Categoría: {names.pop()} ({_names(applied)})."
    return "Categoría: " + " · ".join(f"{c.name} ({p.label})" for p, c in applied.items()) + "."
