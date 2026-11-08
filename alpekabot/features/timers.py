"""Timers: mensajes automáticos cada X minutos, solo en directo y con chat activo."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Callable

from ..core.event_bus import EventBus
from ..core.models import ALL_PLATFORMS, ChatMessage, Platform, StreamInfo, platforms_from_str, platforms_to_str
from ..platforms.base import PlatformError
from ..storage.database import Database, utc_now_iso
from .variables import Fetcher, RenderContext, render

if TYPE_CHECKING:
    from ..bot import AlpekaBot

log = logging.getLogger(__name__)

TICK_S = 20
MAX_MESSAGE_LEN = 500


@dataclass(frozen=True)
class Timer:
    name: str
    message: str
    interval_min: int = 15
    min_lines: int = 5
    platforms: frozenset[Platform] = ALL_PLATFORMS
    enabled: bool = True
    id: int | None = None


# ------------------------------------------------------------- repositorio
class TimerRepository:
    def __init__(self, db: Database, bus: EventBus) -> None:
        self._db = db
        self._bus = bus

    @staticmethod
    def _from_row(r) -> Timer:
        return Timer(
            id=r["id"],
            name=r["name"],
            message=r["message"],
            interval_min=r["interval_min"],
            min_lines=r["min_lines"],
            platforms=platforms_from_str(r["platforms"]),
            enabled=bool(r["enabled"]),
        )

    def list_all(self) -> list[Timer]:
        rows = self._db.conn.execute("SELECT * FROM timers ORDER BY name").fetchall()
        return [self._from_row(r) for r in rows]

    def add(self, t: Timer) -> Timer:
        self._validate(t)
        now = utc_now_iso()
        try:
            with self._db.conn:
                cur = self._db.conn.execute(
                    "INSERT INTO timers (name, message, interval_min, min_lines, platforms, enabled,"
                    " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                    (t.name.strip(), t.message.strip(), t.interval_min, t.min_lines,
                     platforms_to_str(t.platforms), int(t.enabled), now, now),
                )
        except sqlite3.IntegrityError:
            raise ValueError(f"Ya existe un timer llamado «{t.name.strip()}».") from None
        self._bus.timers_changed.emit()
        return replace(t, id=cur.lastrowid)

    def update(self, t: Timer) -> None:
        if t.id is None:
            raise ValueError("Timer sin id")
        self._validate(t)
        try:
            with self._db.conn:
                self._db.conn.execute(
                    "UPDATE timers SET name=?, message=?, interval_min=?, min_lines=?, platforms=?,"
                    " enabled=?, updated_at=? WHERE id=?",
                    (t.name.strip(), t.message.strip(), t.interval_min, t.min_lines,
                     platforms_to_str(t.platforms), int(t.enabled), utc_now_iso(), t.id),
                )
        except sqlite3.IntegrityError:
            raise ValueError(f"Ya existe un timer llamado «{t.name.strip()}».") from None
        self._bus.timers_changed.emit()

    def delete(self, timer_id: int) -> None:
        with self._db.conn:
            self._db.conn.execute("DELETE FROM timers WHERE id = ?", (timer_id,))
        self._bus.timers_changed.emit()

    @staticmethod
    def _validate(t: Timer) -> None:
        if not t.name.strip():
            raise ValueError("El timer necesita un nombre.")
        if not t.message.strip():
            raise ValueError("El mensaje no puede estar vacío.")
        if not 1 <= t.interval_min <= 1440:
            raise ValueError("El intervalo debe estar entre 1 y 1440 minutos.")
        if not 0 <= t.min_lines <= 10_000:
            raise ValueError("El mínimo de mensajes debe estar entre 0 y 10000.")
        if not t.platforms:
            raise ValueError("Elige al menos una plataforma.")


# ----------------------------------------------------------------- servicio
@dataclass
class _State:
    last_sent: float
    lines: int = 0


class TimerService:
    def __init__(
        self,
        bot: "AlpekaBot",
        repo: TimerRepository,
        clock: Callable[[], float] = time.monotonic,
        fetch: Fetcher | None = None,
    ) -> None:
        self._bot = bot
        self._repo = repo
        self._clock = clock
        self._fetch = fetch
        self._state: dict[tuple[int, Platform], _State] = {}
        self._task: asyncio.Task | None = None
        bot.bus.chat_message.connect(self._on_message)
        bot.bus.stream_status.connect(self._on_stream_status)

    # ---------------------------------------------------------- ciclo de vida
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            try:
                await self.tick()
            except Exception:
                log.exception("Error en los timers")

    # --------------------------------------------------------------- eventos
    def _on_message(self, msg: ChatMessage) -> None:
        client = self._bot.platforms.get(msg.platform)
        if client is not None and msg.user_id == client.bot_user_id:
            return  # los mensajes del bot no cuentan como actividad
        for (_, platform), st in self._state.items():
            if platform is msg.platform:
                st.lines += 1

    def _on_stream_status(self, info: StreamInfo) -> None:
        # Al empezar (o terminar) un directo, todos los timers vuelven a esperar su intervalo
        now = self._clock()
        for key in [k for k in self._state if k[1] is info.platform]:
            if info.is_live:
                self._state[key] = _State(last_sent=now)
            else:
                del self._state[key]

    # ----------------------------------------------------------------- lógica
    async def tick(self) -> list[tuple[Platform, str]]:
        """Envía los timers que tocan. Devuelve lo enviado (útil en tests)."""
        now = self._clock()
        sent: list[tuple[Platform, str]] = []
        busy: set[Platform] = set()  # máx. un timer por plataforma y ronda
        timers = [t for t in self._repo.list_all() if t.enabled]
        valid_keys = {(t.id, p) for t in timers for p in t.platforms}
        for key in [k for k in self._state if k not in valid_keys]:
            del self._state[key]  # timers borrados, desactivados o sin esa plataforma

        for t in timers:
            for platform in sorted(t.platforms, key=lambda p: p.value):
                client = self._bot.platforms.get(platform)
                if client is None or not client.is_connected or not client.stream_info.is_live:
                    continue
                st = self._state.setdefault((t.id, platform), _State(last_sent=now))
                if platform in busy:
                    continue
                if now - st.last_sent < t.interval_min * 60 or st.lines < t.min_lines:
                    continue
                ctx = RenderContext(platform=platform, channel=client.channel_name, stream=client.stream_info)
                text = (await render(t.message, ctx, self._fetch))[:MAX_MESSAGE_LEN]
                st.last_sent, st.lines = now, 0
                busy.add(platform)
                try:
                    await self._bot.send_message(platform, text)
                    sent.append((platform, text))
                    log.info("Timer «%s» enviado en %s", t.name, platform.label)
                except PlatformError as exc:
                    log.warning("Timer «%s» no se pudo enviar: %s", t.name, exc)
        return sent
