"""Texto a voz para Twitch y Kick: ``!tts mensaje`` (el nombre del comando se puede cambiar).

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

import edge_tts

from ..config import TtsConfig
from ..core.event_bus import EventBus
from ..core.models import ChatMessage, Permission, Platform
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
# Sanciones que retiran de la cola lo que ese usuario tuviera pendiente
_PUNISHMENTS = {"warn", "delete", "timeout", "ban"}
_REPEATS_RE = re.compile(r"(.)\1{3,}", re.DOTALL)

# (texto, voz, velocidad en %) -> audio MP3
Synthesizer = Callable[[str, str, int], Awaitable[bytes]]


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


class UnreadableText(Exception):
    """La voz no sabe pronunciar nada del texto (p. ej. todo en japonés con una voz española)."""


async def edge_synthesize(text: str, voice: str, rate: int) -> bytes:
    """Genera el audio con el servicio de Edge. edge-tts ya escapa el texto para SSML."""
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
        self._last_used: float | None = None
        self._wake: asyncio.Event | None = None
        self._worker: asyncio.Task | None = None
        self._speaking: asyncio.Task | None = None
        bus.mod_action.connect(self._on_mod_action)

    # ------------------------------------------------------------- estado
    @property
    def current(self) -> TtsItem | None:
        return self._current

    @property
    def pending(self) -> list[TtsItem]:
        return list(self._queue)

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
            self._commands.unregister(self.config.command)
            self._commands.register(name, self._cmd_tts, Permission(self.config.permission))
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
            if key == "command" or not hasattr(self.config, key):
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

    async def _cmd_tts(self, msg: ChatMessage, args: list[str]) -> str | None:
        cfg = self.config
        if not cfg.enabled:
            return None
        now = self._clock()
        if self._last_used is not None and now - self._last_used < cfg.cooldown_s:
            return None
        at = f"@{msg.user_name}"
        text = clean_text(" ".join(args), cfg.max_chars)
        if not text:
            return f"{at} Uso: {PREFIX}{cfg.command} mensaje"
        if len(self._queue) >= cfg.max_queue:
            return f"{at} La cola del TTS está llena, prueba en un rato."
        self._last_used = now
        self._enqueue(text, msg.user_name, msg.user_id, msg.platform)
        return None

    async def _cmd_skip(self, _msg: ChatMessage, _args: list[str]) -> str | None:
        self.skip()
        return None

    # ------------------------------------------------------------- cola
    def _enqueue(self, text: str, user_name: str = "", user_id: str = "", platform: Platform | None = None) -> None:
        spoken = _spoken(text, user_name, self.config.read_name)
        self._queue.append(TtsItem(self._next_id, text, spoken, user_name, user_id, platform))
        self._next_id += 1
        if self._wake is not None:
            self._wake.set()
        self.bus.tts_changed.emit()

    def preview(self, text: str) -> bool:
        """Botón «Probar» de la app: no exige que el TTS esté activado ni aplica el cooldown."""
        text = clean_text(text, self.config.max_chars)
        if text:
            self._enqueue(text)
        return bool(text)

    def skip(self) -> None:
        """Calla el mensaje actual; sigue con el siguiente."""
        if self._speaking is not None and not self._speaking.done():
            self._speaking.cancel()

    def remove(self, item_id: int) -> None:
        self._queue = deque(i for i in self._queue if i.id != item_id)
        self.bus.tts_changed.emit()

    def clear(self) -> None:
        self._queue.clear()
        self.bus.tts_changed.emit()

    def _on_mod_action(self, action: ModAction) -> None:
        """Un mensaje de alguien recién sancionado no se debe leer en el directo."""
        if action.action not in _PUNISHMENTS:
            return
        same = lambda i: i.platform is action.platform and i.user_id == action.user_id  # noqa: E731
        if self._current is not None and same(self._current):
            self.skip()
        if any(same(i) for i in self._queue):
            self._queue = deque(i for i in self._queue if not same(i))
            self.bus.tts_changed.emit()

    # ------------------------------------------------------------- bucle
    def start(self) -> None:
        if self._worker is None:
            self._wake = asyncio.Event()
            if self._queue:
                self._wake.set()
            self._worker = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        self._queue.clear()
        self.skip()
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        if self._speaking is not None:
            await asyncio.gather(self._speaking, return_exceptions=True)

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

    async def _speak(self, item: TtsItem) -> None:
        cfg = self.config
        try:
            if self._player is None:
                raise RuntimeError("sin reproductor")
            audio = await asyncio.wait_for(self._synthesize(item.spoken, cfg.voice, cfg.rate), SYNTH_TIMEOUT_S)
            await self._player.play(audio, cfg.volume, cfg.output_device)
            self.last_error = ""
        except asyncio.CancelledError:
            raise
        except UnreadableText:
            # No es un fallo de la app: se omite sin marcar error en la página del TTS
            who = f" de {item.user_name}" if item.user_name else ""
            log.info("TTS: se omite el mensaje%s: la voz %s no sabe leer ese idioma o alfabeto", who, cfg.voice)
        except Exception as exc:  # sin internet, voz inexistente, servicio de Edge caído...
            self.last_error = f"No se pudo leer un mensaje ({type(exc).__name__}). Revisa internet y la voz elegida."
            log.warning("TTS: %s", self.last_error)
