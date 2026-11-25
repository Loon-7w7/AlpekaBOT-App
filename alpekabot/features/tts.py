"""Texto a voz para Twitch y Kick. Modos (``TtsConfig.modes``, se cambian con ``set_mode``):

- ``command``: ``!tts mensaje`` (el nombre del comando se puede cambiar);
- ``reward``: se lee el texto de un canje de puntos. Si el premio lo creó la app, el canje se
  completa al leerlo y se devuelven los puntos si no llega a leerse;
- ``chat``: se lee todo el chat salvo los comandos (``!algo``).

``command`` y ``reward`` pueden ir juntos; ``chat`` va siempre solo. Los canjes no cuentan
para la cola máxima (quien pagó puntos no se queda fuera por los ``!tts`` gratis), pero
tienen su propio tope: ``REWARD_QUEUE_FACTOR`` veces la cola.

Los mensajes entran en una cola y suenan uno tras otro. La voz la genera el servicio de
Microsoft Edge (librería ``edge-tts``: gratis, sin clave de API, necesita internet) y el
audio lo reproduce ``ui/tts_player.py`` por el dispositivo elegido en la app.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable, Protocol

from ..config import TtsConfig
from ..core.event_bus import EventBus
from ..platforms.base import PlatformError
from ..core.models import ChannelEvent, ChatMessage, EventKind, Permission, Platform
from .commands import PREFIX, is_valid_name
from .moderation import ModAction, remove_links

if TYPE_CHECKING:
    from .commands import CommandService

log = logging.getLogger(__name__)

DEFAULT_COMMAND = "tts"
SKIP_COMMAND = "skiptts"
SYNTH_TIMEOUT_S = 20
MIN_RATE, MAX_RATE = -50, 50
MIN_CHARS, MAX_CHARS = 10, 500
MIN_QUEUE, MAX_QUEUE = 1, 50
MAX_COOLDOWN_S = 3600
MODE_COMMAND, MODE_CHAT, MODE_REWARD = "command", "chat", "reward"
MODES = (MODE_COMMAND, MODE_REWARD, MODE_CHAT)
SOURCE_PREVIEW = "preview"  # TtsItem.source del botón «Probar»
REWARD_QUEUE_FACTOR = 4
MAX_COOLDOWN_USERS = 5000
RESOLVE_TIMEOUT_S = 5
# Sanciones que retiran de la cola lo que ese usuario tuviera pendiente
_PUNISHMENTS = {"warn", "delete", "timeout", "ban"}
_REPEATS_RE = re.compile(r"(.)\1{3,}", re.DOTALL)
_KICK_EMOTE_RE = re.compile(r"\[emote:\d+:[^\]]*\]")

# (texto, voz, velocidad en %) -> audio MP3
Synthesizer = Callable[[str, str, int], Awaitable[bytes]]
# (plataforma, id del premio, id del canje, devolver) -> completa o cancela el canje
CompleteRedemption = Callable[[Platform, str, str, bool], Awaitable[None]]


class TtsPlayer(Protocol):
    async def play(self, audio: bytes, volume: int, device_id: str) -> None:
        """Reproduce y termina cuando acaba el audio. Si se cancela, debe callarse."""

    def set_volume(self, volume: int) -> None: ...


@dataclass(frozen=True)
class Voice:
    name: str     # «es-MX-DaliaNeural»
    label: str    # «es-MX · Dalia (mujer)»


@dataclass(frozen=True)
class TtsItem:
    id: int
    text: str                      # lo que se muestra en la cola
    spoken: str                    # lo que se lee (puede incluir el nombre)
    user_name: str = ""
    user_id: str = ""
    platform: Platform | None = None
    # canje de un premio de la app pendiente de completar o devolver
    reward_id: str = ""
    redemption_id: str = ""
    source: str = ""  # MODE_* o SOURCE_PREVIEW: de dónde vino


def normalize_modes(modes) -> list[str]:
    """Limpia la lista de config.json: sin desconocidos ni repetidos, «chat» solo, nunca vacía."""
    valid = [m for m in MODES if isinstance(modes, (list, tuple)) and m in modes]
    if MODE_CHAT in valid:
        return [MODE_CHAT]
    return valid or [MODE_COMMAND]


def reward_links(config: TtsConfig) -> dict[Platform, dict]:
    """Premios del modo canje por plataforma (ignora entradas mal formadas de config.json)."""
    links = {}
    for raw in config.rewards or []:
        if isinstance(raw, dict) and raw.get("id") and raw.get("platform") in {p.value for p in Platform}:
            links.setdefault(Platform(raw["platform"]), raw)
    return links


class UnreadableText(Exception):
    """La voz no sabe pronunciar nada del texto (p. ej. todo en japonés con una voz española)."""


async def edge_synthesize(text: str, voice: str, rate: int) -> bytes:
    """Genera el audio con el servicio de Edge. edge-tts ya escapa el texto para SSML."""
    import edge_tts  # se carga al usarlo: importarlo tarda ~0,5 s al abrir la app

    communicate = edge_tts.Communicate(text, voice, rate=f"{rate:+d}%")
    try:
        chunks = [c["data"] async for c in communicate.stream() if c["type"] == "audio"]
    except edge_tts.exceptions.NoAudioReceived:
        # Edge no devuelve audio si ninguna palabra está en un alfabeto que la voz conozca
        # (un texto mezclado sí suena: la voz se salta lo que no entiende)
        raise UnreadableText from None
    return b"".join(chunks)


def voice_label(short_name: str, gender: str = "") -> str:
    """'es-MX-DaliaNeural', 'Female' -> 'es-MX · Dalia (mujer)'."""
    parts = short_name.removesuffix("Neural").split("-")
    locale, name = "-".join(parts[:2]), "-".join(parts[2:]) or short_name
    who = {"Female": " (mujer)", "Male": " (hombre)"}.get(gender, "")
    return f"{locale} · {name}{who}"


async def edge_voices() -> list[Voice]:
    """Voces disponibles, con las de español primero."""
    import edge_tts

    voices = await edge_tts.list_voices()
    voices.sort(key=lambda v: (not v["ShortName"].startswith("es-"), v["ShortName"]))
    return [Voice(v["ShortName"], voice_label(v["ShortName"], v.get("Gender", ""))) for v in voices]


def clean_text(text: str, max_chars: int = MAX_CHARS) -> str:
    """Prepara el mensaje de un viewer para leerlo. Devuelve "" si no queda nada legible.

    Quita enlaces y caracteres invisibles, acorta las letras repetidas («aaaaaaa») y corta
    por el último espacio si se pasa de ``max_chars``.
    """
    text = "".join(" " if unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") else c for c in text)
    text = remove_links(text)
    text = _REPEATS_RE.sub(r"\1\1\1", text)
    text = " ".join(text.split())
    if len(text) > max_chars:
        cut = text[:max_chars]
        head, space, _tail = cut.rpartition(" ")
        text = head if space and head else cut
    return text if any(c.isalnum() for c in text) else ""


def remove_emotes(text: str, emotes=()) -> str:
    """Quita los emotes de la plataforma: las palabras que coinciden con su nombre y los códigos de Kick."""
    text = _KICK_EMOTE_RE.sub(" ", text)
    names = set(emotes)
    if not names:
        return text
    return " ".join(w for w in text.split() if w not in names)


def _spoken(text: str, user_name: str, read_name: bool) -> str:
    if not read_name or not user_name:
        return text
    return f"{clean_text(user_name.replace('_', ' ')) or user_name} dice: {text}"


class TtsService:
    def __init__(
        self,
        bus: EventBus,
        config: TtsConfig,
        synthesize: Synthesizer = edge_synthesize,
        voices: Callable[[], Awaitable[list[Voice]]] = edge_voices,
        save_config: Callable[[], None] = lambda: None,
        clock: Callable[[], float] = time.monotonic,
        may_read: Callable[[ChatMessage], bool] = lambda _m: True,
        complete: CompleteRedemption | None = None,
        is_managed: Callable[[Platform, str], bool] = lambda _p, _r: False,
    ) -> None:
        self.bus = bus
        self.config = config
        self.last_error = ""
        self._synthesize = synthesize
        self._voices = voices
        self._save_config = save_config
        self._clock = clock
        self._player: TtsPlayer | None = None
        self._commands: "CommandService | None" = None
        self._queue: deque[TtsItem] = deque()
        self._current: TtsItem | None = None
        self._next_id = 1
        self._may_read = may_read
        self._complete = complete
        self._is_managed = is_managed
        self._last_used: dict[tuple[Platform | None, str], float] = {}  # cooldown por usuario
        self._wake: asyncio.Event | None = None
        self._worker: asyncio.Task | None = None
        self._speaking: asyncio.Task | None = None
        self._resolving: set[asyncio.Future] = set()
        self.config.modes = normalize_modes(self.config.modes)
        bus.mod_action.connect(self._on_mod_action)
        # Se conecta después de la moderación (creada antes): así sabe qué mensajes bloqueó
        bus.chat_message.connect(self._on_chat)
        bus.channel_event.connect(self._on_channel_event)

    # ------------------------------------------------------------- estado
    @property
    def current(self) -> TtsItem | None:
        return self._current

    @property
    def pending(self) -> list[TtsItem]:
        return list(self._queue)

    def has_mode(self, mode: str) -> bool:
        return mode in self.config.modes

    @property
    def command_enabled(self) -> bool:
        """El comando del TTS no está apagado en Comandos → Comandos del sistema."""
        return self._commands is None or self._commands.is_enabled(self.config.command)

    def attach_player(self, player: TtsPlayer) -> None:
        self._player = player

    def list_voices(self) -> Awaitable[list[Voice]]:
        return self._voices()

    # ------------------------------------------------------------- comandos
    def register_commands(self, commands: "CommandService") -> None:
        self._commands = commands
        name = self.config.command
        if not is_valid_name(name) or name == SKIP_COMMAND or commands.is_builtin(name):
            # config.json editado a mano: no se debe pisar otro comando interno como !skip
            log.warning("TTS: el comando !%s no es válido o ya existe; se usa !%s", name, DEFAULT_COMMAND)
            name = self.config.command = DEFAULT_COMMAND
        elif commands.is_taken(name):
            log.warning("TTS: el comando personalizado !%s queda oculto por el del TTS", name)
        commands.register(name, self._cmd_tts, Permission(self.config.permission))
        commands.register(SKIP_COMMAND, self._cmd_skip, Permission.MODERATOR)

    def set_command(self, raw: str) -> str:
        """Cambia el nombre del comando. Lanza ValueError con un mensaje para el usuario."""
        name = raw.strip().removeprefix(PREFIX).lower()
        if not is_valid_name(name):
            raise ValueError("El nombre solo puede tener letras, números y _ (máx. 32).")
        if name == self.config.command:
            return name
        if self._commands is not None:
            if self._commands.is_taken(name):
                raise ValueError(f"!{name} ya es otro comando.")
            old = self.config.command
            was_enabled = self._commands.is_enabled(old)
            self._commands.unregister(old)
            self._commands.register(name, self._cmd_tts, Permission(self.config.permission))
            if not was_enabled:  # el interruptor de la página Comandos sigue al nuevo nombre
                self._commands.set_enabled(old, True, save=False)
                self._commands.set_enabled(name, False, save=False)
        self.config.command = name
        self._save_config()
        self.bus.commands_changed.emit()
        self.bus.tts_changed.emit()
        return name

    def update(self, **changes) -> None:
        """Cambia y guarda ajustes (los valores numéricos se ajustan a su rango)."""
        limits = {"rate": (MIN_RATE, MAX_RATE), "volume": (0, 100), "cooldown_s": (0, MAX_COOLDOWN_S),
                  "max_chars": (MIN_CHARS, MAX_CHARS), "max_queue": (MIN_QUEUE, MAX_QUEUE)}
        for key, value in changes.items():
            if key in ("command", "modes") or not hasattr(self.config, key):
                raise ValueError(f"Ajuste desconocido: {key}")
            if key in limits:
                low, high = limits[key]
                value = max(low, min(high, int(value)))
            setattr(self.config, key, value)
        if "permission" in changes and self._commands is not None:
            self._commands.register(self.config.command, self._cmd_tts, Permission(self.config.permission))
            self.bus.commands_changed.emit()
        if "volume" in changes and self._player is not None:
            self._player.set_volume(self.config.volume)
        self._save_config()
        self.bus.tts_changed.emit()

    def set_mode(self, mode: str, on: bool) -> None:
        """Enciende o apaga un modo. «chat» va solo: al encenderlo apaga los otros y al
        apagarlo se vuelve a «command». Lanza ValueError con un mensaje para el usuario."""
        if mode not in MODES:
            raise ValueError(f"Modo desconocido: {mode}")
        if mode == MODE_CHAT:
            new = [MODE_CHAT] if on else [MODE_COMMAND]
        else:
            rest = {m for m in self.config.modes if m != MODE_CHAT}
            new = [m for m in MODES if m in (rest | {mode} if on else rest - {mode})]
        if not new:
            raise ValueError("Tiene que quedar al menos un modo encendido.")
        if new == self.config.modes:
            return
        self.config.modes = new
        if MODE_CHAT not in new:  # lo que quedaba del chat ya no se lee
            self._drop(lambda i: i.source == MODE_CHAT)
        self._save_config()
        self.bus.tts_changed.emit()

    def set_reward(self, platform: Platform, reward_id: str = "", title: str = "") -> None:
        """Elige el premio del modo canje en una plataforma ("" = ninguno)."""
        links = reward_links(self.config)
        if reward_id:
            links[platform] = {"platform": platform.value, "id": reward_id, "title": title}
        else:
            links.pop(platform, None)
        new = [links[p] for p in Platform if p in links]
        if new != self.config.rewards:
            self.config.rewards = new
            self._save_config()
            self.bus.tts_changed.emit()

    # --------------------------------------------------- cooldown por usuario
    def _cooling(self, platform: Platform | None, user_id: str) -> bool:
        last = self._last_used.get((platform, user_id))
        return last is not None and self._clock() - last < self.config.cooldown_s

    def _mark_used(self, platform: Platform | None, user_id: str) -> None:
        now = self._clock()
        if len(self._last_used) >= MAX_COOLDOWN_USERS:  # se olvidan los que ya no esperan
            limit = self.config.cooldown_s
            self._last_used = {k: v for k, v in self._last_used.items() if now - v < limit}
        self._last_used[(platform, user_id)] = now

    # ------------------------------------------------------------- entradas
    async def _cmd_tts(self, msg: ChatMessage, args: list[str]) -> str | None:
        cfg = self.config
        if not cfg.enabled or not self.has_mode(MODE_COMMAND):
            return None
        if self._cooling(msg.platform, msg.user_id):
            return None
        at = f"@{msg.user_name}"
        raw = " ".join(args)
        text = clean_text(remove_emotes(raw, msg.emotes) if cfg.skip_emotes else raw, cfg.max_chars)
        if not text:
            return f"{at} Uso: {PREFIX}{cfg.command} mensaje"
        if self._queue_full():
            return f"{at} La cola del TTS está llena, prueba en un rato."
        self._mark_used(msg.platform, msg.user_id)
        self._enqueue(text, msg.user_name, msg.user_id, msg.platform, source=MODE_COMMAND)
        return None

    def _on_chat(self, msg: ChatMessage) -> None:
        """Modo «todo el chat»: cualquier mensaje que no sea un comando."""
        cfg = self.config
        if not cfg.enabled or not self.has_mode(MODE_CHAT) or msg.text.lstrip().startswith(PREFIX):
            return
        if not self._may_read(msg) or self._cooling(msg.platform, msg.user_id):
            return  # bloqueado por un filtro, del propio bot o en cooldown
        raw = remove_emotes(msg.text, msg.emotes) if cfg.skip_emotes else msg.text
        text = clean_text(raw, cfg.max_chars)
        if not text or self._queue_full():
            return  # en el chat no se responde: sería spam
        self._mark_used(msg.platform, msg.user_id)
        self._enqueue(text, msg.user_name, msg.user_id, msg.platform, source=MODE_CHAT)

    def _on_channel_event(self, ev: ChannelEvent) -> None:
        """Modo canje: el texto que el viewer escribió al canjear el premio."""
        if ev.kind is not EventKind.REWARD:
            return
        link = reward_links(self.config).get(ev.platform)
        if link is None or link["id"] != ev.reward_id:
            return
        cfg = self.config
        managed = ev.pending and bool(ev.redemption_id) and self._is_managed(ev.platform, ev.reward_id)
        text = clean_text(remove_emotes(ev.user_input) if cfg.skip_emotes else ev.user_input, cfg.max_chars)
        reason = ("el TTS está desactivado" if not cfg.enabled
                  else "el modo canje está apagado" if not self.has_mode(MODE_REWARD)
                  else "el mensaje no tiene nada legible" if not text
                  else "hay demasiados canjes en cola"
                  if self._count(MODE_REWARD) >= cfg.max_queue * REWARD_QUEUE_FACTOR else "")
        if reason:
            log.info("TTS: no se lee el canje de %s: %s%s", ev.user_name or "alguien", reason,
                     "; se devuelven los puntos" if managed else "")
            if managed:
                self._resolve(ev.platform, ev.reward_id, ev.redemption_id, refund=True)
            return
        self._enqueue(text, ev.user_name, ev.user_id, ev.platform,
                      ev.reward_id if managed else "", ev.redemption_id if managed else "", MODE_REWARD)

    async def _cmd_skip(self, _msg: ChatMessage, _args: list[str]) -> str | None:
        self.skip()
        return None

    # ------------------------------------------------------------- cola
    def _count(self, source: str) -> int:
        return sum(1 for i in self._queue if i.source == source)

    def _queue_full(self) -> bool:
        """Cola máxima para !tts y el chat: los canjes no cuentan (tienen su propio tope)."""
        return len(self._queue) - self._count(MODE_REWARD) >= self.config.max_queue

    def _enqueue(self, text: str, user_name: str = "", user_id: str = "", platform: Platform | None = None,
                 reward_id: str = "", redemption_id: str = "", source: str = "") -> None:
        spoken = _spoken(text, user_name, self.config.read_name)
        self._queue.append(TtsItem(self._next_id, text, spoken, user_name, user_id, platform,
                                   reward_id, redemption_id, source))
        self._next_id += 1
        if self._wake is not None:
            self._wake.set()
        self.bus.tts_changed.emit()

    def preview(self, text: str) -> bool:
        """Botón «Probar» de la app: no exige que el TTS esté activado ni aplica el cooldown."""
        text = clean_text(text, self.config.max_chars)
        if text:
            self._enqueue(text, source=SOURCE_PREVIEW)
        return bool(text)

    def skip(self) -> None:
        """Calla el mensaje actual; sigue con el siguiente."""
        if self._speaking is not None and not self._speaking.done():
            self._speaking.cancel()

    def remove(self, item_id: int) -> None:
        self._drop(lambda i: i.id == item_id)

    def clear(self) -> None:
        self._drop(lambda _i: True)

    def _drop(self, which: Callable[[TtsItem], bool]) -> None:
        """Saca de la cola lo que no se va a leer (y devuelve los puntos de sus canjes)."""
        gone = [i for i in self._queue if which(i)]
        if not gone:
            return
        self._queue = deque(i for i in self._queue if not which(i))
        for item in gone:
            self._refund(item)
        self.bus.tts_changed.emit()

    # ------------------------------------------------------------- canjes
    def _refund(self, item: TtsItem) -> None:
        if item.redemption_id and item.platform is not None:
            self._resolve(item.platform, item.reward_id, item.redemption_id, refund=True)

    def _resolve(self, platform: Platform, reward_id: str, redemption_id: str, refund: bool) -> None:
        if self._complete is None:
            return
        task = asyncio.ensure_future(self._safe_complete(platform, reward_id, redemption_id, refund))
        self._resolving.add(task)
        task.add_done_callback(self._resolving.discard)

    async def _safe_complete(self, platform: Platform, reward_id: str, redemption_id: str, refund: bool) -> None:
        what = "devolver" if refund else "completar"
        try:
            await self._complete(platform, reward_id, redemption_id, refund)
        except PlatformError as exc:  # su mensaje ya es apto para el usuario
            log.warning("TTS: no se pudo %s el canje (%s)", what, exc)
        except Exception as exc:
            log.warning("TTS: no se pudo %s el canje (%s)", what, type(exc).__name__)

    def _on_mod_action(self, action: ModAction) -> None:
        """Un mensaje de alguien recién sancionado no se debe leer en el directo."""
        if action.action not in _PUNISHMENTS:
            return
        same = lambda i: i.platform is action.platform and i.user_id == action.user_id  # noqa: E731
        if self._current is not None and same(self._current):
            self.skip()
        self._drop(same)

    # ------------------------------------------------------------- bucle
    def start(self) -> None:
        if self._worker is None:
            self._wake = asyncio.Event()
            if self._queue:
                self._wake.set()
            self._worker = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        self._drop(lambda _i: True)  # los canjes que no se leyeron recuperan sus puntos
        self.skip()
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        if self._speaking is not None:
            await asyncio.gather(self._speaking, return_exceptions=True)
        if self._resolving:
            await asyncio.wait(set(self._resolving), timeout=RESOLVE_TIMEOUT_S)

    async def _run(self) -> None:
        while True:
            await self._wake.wait()
            if not self._queue:
                self._wake.clear()
                continue
            item = self._queue.popleft()
            self._current = item
            self.bus.tts_changed.emit()
            # Tarea aparte: «saltar» cancela solo este mensaje, no el bucle
            self._speaking = asyncio.ensure_future(self._speak(item))
            try:
                await asyncio.wait({self._speaking})
            finally:
                self._current = None
                self.bus.tts_changed.emit()
                self._settle(item, self._speaking)

    def _settle(self, item: TtsItem, task: asyncio.Task) -> None:
        """Al terminar un canje: se completa si se leyó (aunque lo saltaran) y si no, se devuelve."""
        if not item.redemption_id:
            return
        failed = task.done() and not task.cancelled() and task.exception() is None and task.result() is False
        if failed:
            self._refund(item)
        elif item.platform is not None:
            self._resolve(item.platform, item.reward_id, item.redemption_id, refund=False)

    async def _speak(self, item: TtsItem) -> bool:
        """Lee un mensaje. Devuelve False si no se pudo leer."""
        cfg = self.config
        try:
            if self._player is None:
                raise RuntimeError("sin reproductor")
            audio = await asyncio.wait_for(self._synthesize(item.spoken, cfg.voice, cfg.rate), SYNTH_TIMEOUT_S)
            await self._player.play(audio, cfg.volume, cfg.output_device)
            self.last_error = ""
            return True
        except asyncio.CancelledError:
            raise
        except UnreadableText:
            # No es un fallo de la app: se omite sin marcar error en la página del TTS
            who = f" de {item.user_name}" if item.user_name else ""
            log.info("TTS: se omite el mensaje%s: la voz %s no sabe leer ese idioma o alfabeto", who, cfg.voice)
        except Exception as exc:  # sin internet, voz inexistente, servicio de Edge caído...
            self.last_error = f"No se pudo leer un mensaje ({type(exc).__name__}). Revisa internet y la voz elegida."
            log.warning("TTS: %s", self.last_error)
        return False
