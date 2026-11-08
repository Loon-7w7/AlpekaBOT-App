"""Interfaz común que implementa cada plataforma (Twitch, Kick)."""

from __future__ import annotations

import asyncio
import logging
import webbrowser
from abc import ABC, abstractmethod

from ..core.event_bus import EventBus
from ..core.models import ConnectionState, Platform, Reward, StreamInfo

log = logging.getLogger(__name__)


class PlatformError(Exception):
    """Error con un mensaje apto para mostrar al usuario."""


async def open_in_browser(url: str) -> None:
    """Abre una URL en el navegador SIN bloquear ni reentrar en el hilo de Qt.

    En Windows, webbrowser.open() usa ShellExecute, que procesa mensajes de Windows
    mientras abre el navegador. Llamado desde una tarea asyncio en el hilo de Qt
    (qasync), eso ejecuta otras tareas dentro de la actual y Python lanza
    "RuntimeError: Cannot enter into task ... while another task is being executed".
    Por eso se ejecuta en un hilo aparte.
    """
    await asyncio.get_running_loop().run_in_executor(None, webbrowser.open, url)


class PlatformClient(ABC):
    platform: Platform

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._state = ConnectionState.DISCONNECTED
        self.stream_info = StreamInfo(self.platform)

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def is_configured(self) -> bool:
        """False si el usuario no ha configurado esta plataforma (se omite al conectar)."""
        return True

    @property
    def bot_user_id(self) -> str | None:
        """ID de la cuenta del bot (para ignorar sus propios mensajes)."""
        return None

    @property
    def channel_name(self) -> str:
        """Nombre del canal donde trabaja el bot."""
        return ""

    def _set_stream(self, info: StreamInfo) -> None:
        if info != self.stream_info:
            if info.is_live != self.stream_info.is_live:
                log.info("%s: %s", self.platform.label, "EN DIRECTO" if info.is_live else "fuera de directo")
            self.stream_info = info
            self._bus.stream_status.emit(info)

    @property
    def is_connected(self) -> bool:
        return self._state is ConnectionState.CONNECTED

    def _set_state(self, state: ConnectionState, detail: str = "") -> None:
        self._state = state
        log.info("%s: %s %s", self.platform.label, state.label, detail)
        self._bus.connection_state.emit(self.platform, state, detail)

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def send_message(self, text: str, reply_to_id: str | None = None) -> None: ...

    # ------------------------------------------------------------ moderación
    # Las plataformas que lo soporten las sobrescriben. Lanzan PlatformError con
    # un mensaje apto para el usuario.
    async def delete_message(self, message_id: str) -> None:
        raise PlatformError(f"{self.platform.label} no permite borrar mensajes")

    async def timeout_user(self, user_id: str, seconds: int, reason: str = "") -> None:
        raise PlatformError(f"{self.platform.label} no permite timeouts")

    async def ban_user(self, user_id: str, reason: str = "") -> None:
        raise PlatformError(f"{self.platform.label} no permite banear")

    async def unban_user(self, user_id: str) -> None:
        raise PlatformError(f"{self.platform.label} no permite desbanear")

    async def lookup_user(self, login: str) -> tuple[str, str] | None:
        """Busca un usuario por su nombre: (user_id, nombre visible) o None si no existe."""
        return None

    # -------------------------------------------------- premios (fase 7)
    async def list_rewards(self) -> list[Reward]:
        """Premios de puntos de canal del streamer (``managed`` lo rellena quien llama)."""
        raise PlatformError(f"{self.platform.label} no tiene premios")

    async def create_reward(self, title: str, cost: int, prompt: str = "") -> Reward:
        raise PlatformError(f"{self.platform.label} no permite crear premios")

    async def delete_reward(self, reward_id: str) -> None:
        raise PlatformError(f"{self.platform.label} no permite borrar premios")

    async def complete_redemption(self, reward_id: str, redemption_id: str, refund: bool) -> None:
        """Marca un canje como hecho o lo cancela devolviendo los puntos (solo premios de la app)."""
        raise PlatformError(f"{self.platform.label} no permite gestionar canjes")
