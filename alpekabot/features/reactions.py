"""Reacciones del avatar (VTube Studio) a canjes de premios y eventos del canal.

Reglas acordadas con el usuario (fase 7):
- Disparadores: canjes de premios (Twitch y Kick) y eventos: follow, sub, renovación,
  subs regaladas (un regalo de N = UNA reacción), bits/kicks y raids (cantidad mínima).
- Una reacción = lista de pasos (hotkey de VTS, expresión durante X s, esperar) más un
  mensaje opcional al chat con variables ($user, $amount, $reward, $input...).
- Cooldown global por reacción (se aplica al encolar). Las reacciones se ejecutan EN COLA,
  una detrás de otra.
- Si VTube Studio no está conectado, la reacción se descarta (aviso en el log). Si era un
  canje de un premio creado por la app, se devuelven los puntos.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field, replace
from typing import Awaitable, Callable

from ..core.event_bus import EventBus
from ..core.models import (
    ALL_PLATFORMS,
    ChannelEvent,
    EventKind,
    Platform,
    StreamInfo,
    platforms_from_str,
    platforms_to_str,
)
from ..integrations.vtube_studio import VTSError
from ..platforms.base import PlatformError
from ..storage.database import Database, utc_now_iso
from .variables import RenderContext, render

log = logging.getLogger(__name__)

MAX_NAME_LEN = 40
MAX_STEPS = 20
MAX_STEP_S = 120          # duración máxima de una expresión o una espera
MAX_MESSAGE_LEN = 500
MAX_QUEUE = 50
HOTKEY, EXPRESSION, WAIT = "hotkey", "expression", "wait"
STEP_KINDS = (HOTKEY, EXPRESSION, WAIT)


@dataclass(frozen=True)
class Step:
    kind: str                 # "hotkey" | "expression" | "wait"
    ref: str = ""             # ID de la hotkey / archivo de la expresión
    name: str = ""            # nombre para mostrar
    seconds: float = 0        # expresión: cuánto dura (0 = se queda); espera: cuánto
    active: bool = True       # expresión: activar (True) o desactivar (False)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "ref": self.ref, "name": self.name, "seconds": self.seconds,
                "active": self.active}

    @classmethod
    def from_dict(cls, d: dict) -> "Step":
        return cls(str(d.get("kind", "")), str(d.get("ref", "")), str(d.get("name", "")),
                   float(d.get("seconds") or 0), bool(d.get("active", True)))

    def describe(self) -> str:
        label = self.name or self.ref
        if self.kind == HOTKEY:
            return f"Hotkey «{label}»"
        if self.kind == EXPRESSION:
            if not self.active:
                return f"Quitar expresión «{label}»"
            return f"Expresión «{label}»" + (f" durante {_secs(self.seconds)}" if self.seconds else "")
        return f"Esperar {_secs(self.seconds)}"


def _secs(value: float) -> str:
    return f"{value:g} s"


@dataclass(frozen=True)
class Reaction:
    name: str
    trigger: EventKind
    steps: tuple[Step, ...] = ()
    message: str = ""
    cooldown_s: int = 0
    platforms: frozenset[Platform] = ALL_PLATFORMS
    reward_id: str = ""        # solo canjes: premio concreto (de UNA plataforma)
    reward_title: str = ""
    min_amount: int = 0        # bits/kicks, nº de subs regaladas, espectadores del raid
    enabled: bool = True
    id: int | None = None

    def matches(self, ev: ChannelEvent) -> bool:
        if not self.enabled or ev.kind is not self.trigger or ev.platform not in self.platforms:
            return False
        if self.trigger is EventKind.REWARD:
            return bool(self.reward_id) and ev.reward_id == self.reward_id
        return ev.amount >= self.min_amount


# ------------------------------------------------------------- repositorio
class ReactionRepository:
    def __init__(self, db: Database, bus: EventBus) -> None:
        self._db = db
        self._bus = bus

    @staticmethod
    def _from_row(r) -> Reaction:
        try:
            steps = tuple(Step.from_dict(d) for d in json.loads(r["steps"]) if isinstance(d, dict))
        except (ValueError, TypeError):
            steps = ()
        return Reaction(
            id=r["id"], name=r["name"], trigger=EventKind(r["trigger"]), steps=steps, message=r["message"],
            cooldown_s=r["cooldown_s"], platforms=platforms_from_str(r["platforms"]),
            reward_id=r["reward_id"], reward_title=r["reward_title"], min_amount=r["min_amount"],
            enabled=bool(r["enabled"]),
        )

    def list_all(self) -> list[Reaction]:
        rows = self._db.conn.execute("SELECT * FROM reactions ORDER BY name").fetchall()
        return [self._from_row(r) for r in rows]

    def _values(self, r: Reaction) -> tuple:
        return (r.name.strip(), r.trigger.value, r.reward_id, r.reward_title, max(0, int(r.min_amount)),
                platforms_to_str(r.platforms), json.dumps([s.to_dict() for s in r.steps], ensure_ascii=False),
                r.message.strip(), int(r.cooldown_s), int(r.enabled))

    def add(self, r: Reaction) -> Reaction:
        r = self._validate(r)
        now = utc_now_iso()
        try:
            with self._db.conn:
                cur = self._db.conn.execute(
                    "INSERT INTO reactions (name, trigger, reward_id, reward_title, min_amount, platforms, steps,"
                    " message, cooldown_s, enabled, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (*self._values(r), now, now),
                )
        except sqlite3.IntegrityError:
            raise ValueError(f"Ya existe una reacción llamada «{r.name}».") from None
        self._bus.reactions_changed.emit()
        return replace(r, id=cur.lastrowid)

    def update(self, r: Reaction) -> None:
        if r.id is None:
            raise ValueError("Reacción sin id")
        r = self._validate(r)
        try:
            with self._db.conn:
                self._db.conn.execute(
                    "UPDATE reactions SET name=?, trigger=?, reward_id=?, reward_title=?, min_amount=?, platforms=?,"
                    " steps=?, message=?, cooldown_s=?, enabled=?, updated_at=? WHERE id=?",
                    (*self._values(r), utc_now_iso(), r.id),
                )
        except sqlite3.IntegrityError:
            raise ValueError(f"Ya existe una reacción llamada «{r.name}».") from None
        self._bus.reactions_changed.emit()

    def delete(self, reaction_id: int) -> None:
        with self._db.conn:
            self._db.conn.execute("DELETE FROM reactions WHERE id = ?", (reaction_id,))
        self._bus.reactions_changed.emit()

    @staticmethod
    def _validate(r: Reaction) -> Reaction:
        name = r.name.strip()
        if not name or len(name) > MAX_NAME_LEN:
            raise ValueError(f"El nombre es obligatorio (máx. {MAX_NAME_LEN} caracteres).")
        if not r.platforms:
            raise ValueError("Elige al menos una plataforma.")
        if r.trigger is EventKind.REWARD:
            if not r.reward_id:
                raise ValueError("Elige el premio que dispara la reacción.")
            if len(r.platforms) != 1:
                raise ValueError("Un premio pertenece a una sola plataforma.")
        if r.trigger is EventKind.RAID and Platform.TWITCH not in r.platforms:
            raise ValueError("Los raids solo existen en Twitch.")
        if not r.steps and not r.message.strip():
            raise ValueError("Añade al menos un paso o un mensaje para el chat.")
        if len(r.steps) > MAX_STEPS:
            raise ValueError(f"Máximo {MAX_STEPS} pasos.")
        for s in r.steps:
            if s.kind not in STEP_KINDS:
                raise ValueError("Paso desconocido.")
            if s.kind in (HOTKEY, EXPRESSION) and not s.ref:
                raise ValueError("Hay un paso sin hotkey o expresión elegida.")
            if not 0 <= s.seconds <= MAX_STEP_S:
                raise ValueError(f"La duración de cada paso debe estar entre 0 y {MAX_STEP_S} s.")
            if s.kind == WAIT and s.seconds <= 0:
                raise ValueError("Una espera necesita una duración.")
        if len(r.message) > MAX_MESSAGE_LEN:
            raise ValueError(f"El mensaje supera {MAX_MESSAGE_LEN} caracteres.")
        if not 0 <= r.cooldown_s <= 86_400:
            raise ValueError("El cooldown debe estar entre 0 y 86400 segundos.")
        return replace(r, name=name)

    # ---------------------------------------------- premios creados por la app
    def add_managed_reward(self, platform: Platform, reward_id: str, title: str) -> None:
        with self._db.conn:
            self._db.conn.execute(
                "INSERT OR REPLACE INTO managed_rewards (platform, reward_id, title, created_at) VALUES (?,?,?,?)",
                (platform.value, reward_id, title, utc_now_iso()),
            )
        self._bus.rewards_changed.emit()

    def remove_managed_reward(self, platform: Platform, reward_id: str) -> None:
        with self._db.conn:
            self._db.conn.execute("DELETE FROM managed_rewards WHERE platform = ? AND reward_id = ?",
                                  (platform.value, reward_id))
        self._bus.rewards_changed.emit()

    def is_managed(self, platform: Platform, reward_id: str) -> bool:
        row = self._db.conn.execute("SELECT 1 FROM managed_rewards WHERE platform = ? AND reward_id = ?",
                                    (platform.value, reward_id)).fetchone()
        return row is not None

    def managed_ids(self, platform: Platform) -> set[str]:
        rows = self._db.conn.execute("SELECT reward_id FROM managed_rewards WHERE platform = ?", (platform.value,))
        return {r[0] for r in rows}


# ----------------------------------------------------------------- servicio
class AvatarControl:
    """Lo que la reacción necesita de VTube Studio (VTubeStudioClient lo cumple)."""

    is_connected: bool

    async def trigger_hotkey(self, hotkey_id: str) -> None: ...

    async def set_expression(self, file: str, active: bool, fade_s: float = 0.25) -> None: ...


SendFn = Callable[[Platform, str], Awaitable[None]]
# (plataforma, id del premio, id del canje, devolver los puntos)
CompleteFn = Callable[[Platform, str, str, bool], Awaitable[None]]
ContextFn = Callable[[Platform], tuple[str, StreamInfo | None]]


@dataclass
class _Job:
    reaction: Reaction
    event: ChannelEvent
    test: bool = False
    queued_at: float = field(default_factory=time.monotonic)


class ReactionService:
    def __init__(
        self,
        bus: EventBus,
        repo: ReactionRepository,
        avatar: AvatarControl,
        send: SendFn,
        complete: CompleteFn,
        context: ContextFn = lambda p: ("", None),
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.repo = repo
        self._avatar = avatar
        self._send = send
        self._complete = complete
        self._context = context
        self._clock = clock
        self._sleep = sleep
        self._last: dict[int, float] = {}       # id de la reacción -> último uso (cooldown)
        self._queue: asyncio.Queue[_Job] | None = None
        self._worker: asyncio.Task | None = None
        bus.channel_event.connect(self.handle)

    # ------------------------------------------------------------- ciclo
    def start(self) -> None:
        if self._worker is None:
            self._queue = asyncio.Queue()
            self._worker = asyncio.ensure_future(self._run_queue())

    async def stop(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            try:
                await worker
            except (asyncio.CancelledError, Exception):
                pass

    @property
    def pending(self) -> int:
        return self._queue.qsize() if self._queue else 0

    # ------------------------------------------------------------- entrada
    def handle(self, ev: ChannelEvent) -> None:
        """Llega un evento del canal (señal bus.channel_event)."""
        log.info("Evento de %s: %s de %s%s", ev.platform.label, ev.kind.label, ev.user_name or "anónimo",
                 f" ({ev.reward_title})" if ev.reward_title else (f" ×{ev.amount}" if ev.amount else ""))
        now = self._clock()
        first = True
        for r in self.repo.list_all():
            if not r.matches(ev) or r.id is None:
                continue
            last = self._last.get(r.id)
            if last is not None and now - last < r.cooldown_s:
                log.info("Reacción «%s» en cooldown: se ignora este evento", r.name)
                continue
            if not self._enqueue(_Job(r, ev if first else replace(ev, pending=False))):
                continue
            self._last[r.id] = now
            first = False  # el canje se completa o devuelve UNA sola vez

    def test(self, r: Reaction) -> None:
        """Prueba una reacción desde la app (sin cooldown y sin mensaje al chat)."""
        platform = next(iter(r.platforms), Platform.TWITCH)
        ev = ChannelEvent(platform, r.trigger, user_name="AlpekaBOT", amount=max(1, r.min_amount),
                          reward_id=r.reward_id, reward_title=r.reward_title)
        self._enqueue(_Job(r, ev, test=True))

    def _enqueue(self, job: _Job) -> bool:
        if self._queue is None:
            log.warning("Reacciones: el servicio no está en marcha")
            return False
        if self._queue.qsize() >= MAX_QUEUE:
            log.warning("Reacciones: la cola está llena (%d); se descarta «%s»", MAX_QUEUE, job.reaction.name)
            return False
        self._queue.put_nowait(job)
        return True

    # ------------------------------------------------------------- ejecución
    async def _run_queue(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                await self.run(job.reaction, job.event, test=job.test)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Error ejecutando la reacción «%s»", job.reaction.name)

    async def run(self, r: Reaction, ev: ChannelEvent, test: bool = False) -> None:
        managed = (ev.kind is EventKind.REWARD and ev.pending and bool(ev.redemption_id)
                   and self.repo.is_managed(ev.platform, ev.reward_id))
        if r.steps and not self._avatar.is_connected:
            log.warning("VTube Studio no está conectado: se descarta la reacción «%s»%s", r.name,
                        " y se devuelven los puntos" if managed else "")
            if managed:
                await self._finish(ev, refund=True)
            return
        done = 0
        try:
            for step in r.steps:
                await self._step(step)
                done += 1
        except VTSError as exc:
            log.warning("Reacción «%s»: %s", r.name, exc)
            if done == 0:
                if managed:
                    await self._finish(ev, refund=True)
                return
        if test:
            log.info("Prueba de «%s» terminada", r.name)
            return
        if r.message.strip():
            await self._say(r, ev)
        if managed:
            await self._finish(ev, refund=False)

    async def _step(self, step: Step) -> None:
        if step.kind == HOTKEY:
            await self._avatar.trigger_hotkey(step.ref)
        elif step.kind == EXPRESSION:
            await self._avatar.set_expression(step.ref, step.active)
            if step.active and step.seconds > 0:
                try:
                    await self._sleep(step.seconds)
                finally:
                    try:
                        await self._avatar.set_expression(step.ref, False)
                    except VTSError:
                        pass
        elif step.kind == WAIT:
            await self._sleep(step.seconds)

    async def _say(self, r: Reaction, ev: ChannelEvent) -> None:
        channel, stream = self._context(ev.platform)
        ctx = RenderContext(
            platform=ev.platform, channel=channel, user_name=ev.user_name or "Anónimo", stream=stream,
            extra={"amount": str(ev.amount), "reward": ev.reward_title, "input": ev.user_input},
        )
        try:
            text = (await render(r.message, ctx))[:MAX_MESSAGE_LEN]
            await self._send(ev.platform, text)
        except PlatformError as exc:
            log.warning("Reacción «%s»: no se pudo enviar el mensaje (%s)", r.name, exc)

    async def _finish(self, ev: ChannelEvent, refund: bool) -> None:
        try:
            await self._complete(ev.platform, ev.reward_id, ev.redemption_id, refund)
        except PlatformError as exc:
            log.warning("No se pudo %s el canje de «%s» (%s)", "devolver" if refund else "completar",
                        ev.reward_title, exc)
