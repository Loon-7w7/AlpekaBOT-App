"""Orquestador del bot: une configuración, plataformas, base de datos y funciones.

La interfaz habla solo con esta clase; nunca directamente con las plataformas.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import aiohttp

from . import credentials
from .config import AppConfig
from .core.event_bus import EventBus
from .core.models import AccountInfo, AccountRole, ChatMessage, ConnectionState, Permission, Platform, Reward
from .features.commands import CommandRepository, CommandService
from .features.moderation import ModerationRepository, ModerationService
from .features.reactions import ReactionRepository, ReactionService
from .integrations.obs_overlay import OverlayServer
from .integrations.vtube_studio import VTubeStudioClient
from .features.songs import SongRepository, SongService
from .features.stream_control import StreamControl
from .features.timers import TimerRepository, TimerService
from .features.tts import TtsService
from .features.updater import Updater
from .features.youtube import API_KEY_SECRET, YouTubeClient
from .paths import DATA_DIR, DB_FILE, ensure_dirs
from .platforms.base import PlatformClient, PlatformError
from .platforms.kick import auth as kick_auth
from .platforms.kick.client import KickClient
from .platforms.twitch import auth as twitch_auth
from .platforms.twitch.client import TwitchClient
from .storage.database import Database

log = logging.getLogger(__name__)

AVATAR_MAX_BYTES = 2 * 1024 * 1024
AVATAR_TIMEOUT_S = 10


async def download_image(url: str) -> bytes | None:
    """Descarga una imagen entera (máx. ``AVATAR_MAX_BYTES``); ``None`` si no es una imagen o es demasiado grande."""
    timeout = aiohttp.ClientTimeout(total=AVATAR_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as http, http.get(url) as response:
        response.raise_for_status()
        if not response.content_type.startswith("image/"):
            return None
        data = b""
        # read(n) devuelve lo que haya llegado, no el archivo entero: hay que leer hasta el final
        while len(data) <= AVATAR_MAX_BYTES and (chunk := await response.content.read(64 * 1024)):
            data += chunk
    return data if len(data) <= AVATAR_MAX_BYTES else None


def avatar_path(platform: Platform) -> Path:
    """Foto de perfil del streamer guardada en disco (sin credenciales: es una imagen pública)."""
    return DATA_DIR / "avatars" / f"{platform.value}.img"

OVERLAY_TOKEN_SECRET = "overlay/token"
LEGACY_NOW_PLAYING = DATA_DIR / "now_playing.txt"  # antiguo archivo para OBS (sustituido por el overlay)


class AlpekaBot:
    def __init__(self, config: AppConfig, db_path: Path | str | None = None) -> None:
        self.config = config
        self.bus = EventBus()
        self.platforms: dict[Platform, PlatformClient] = {
            Platform.TWITCH: TwitchClient(config.twitch, self.bus),
            Platform.KICK: KickClient(config.kick, self.bus),
        }
        # db_path explícito = modo prueba: no se tocan data/config.json ni el keyring del overlay
        real_data = db_path is None
        self._real_data = real_data
        if real_data:
            ensure_dirs()
            db_path = DB_FILE
            twitch_auth.cleanup_legacy_secret()  # de cuando cada usuario ponía su app de Twitch
            LEGACY_NOW_PLAYING.unlink(missing_ok=True)  # si no, OBS seguiría enseñando una canción vieja
        self.db = Database(db_path)
        self.commands = CommandRepository(self.db, self.bus)
        self.timers = TimerRepository(self.db, self.bus)
        save_config = config.save if real_data else (lambda: None)
        # La moderación se crea ANTES que los comandos: revisa cada mensaje primero
        self.moderation = ModerationService(
            self, ModerationRepository(self.db, self.bus), config.moderation, save_config=save_config
        )
        self._command_service = CommandService(
            self, self.commands, disabled=config.commands.disabled_builtins, save_config=save_config
        )
        self.moderation.register_commands(self._command_service)
        self.stream = StreamControl(self.platforms)  # título y categoría (!title, !game, página «Directo»)
        self.stream.register_commands(self._command_service)
        self._timer_service = TimerService(self, self.timers)
        self.songs = SongService(
            self.bus,
            SongRepository(self.db, self.bus),
            config.songs,
            YouTubeClient(lambda: credentials.get_secret(API_KEY_SECRET) or ""),
            save_config=save_config,
            notify=self.send_message,
        )
        self.songs.register_commands(self._command_service)
        self.tts = TtsService(
            self.bus, config.tts, save_config=save_config,
            may_read=self._tts_may_read,
            complete=self._complete_redemption,
            is_managed=lambda p, reward_id: self.reactions.repo.is_managed(p, reward_id),
        )
        self.tts.register_commands(self._command_service)
        self._save_config = save_config
        self.updater = Updater(beta=config.updates.beta)

        # Overlay «Sonando ahora» para OBS
        self.overlay = OverlayServer(config.overlay, _overlay_token() if real_data else None)
        self.bus.song_status.connect(self.overlay.set_status)
        self.overlay.set_status(self.songs.status)

        # Fase 7: VTube Studio + reacciones a canjes y eventos
        self.vtube = VTubeStudioClient(config.vtube, self.bus.vts_status.emit)
        self.reactions = ReactionService(
            self.bus,
            ReactionRepository(self.db, self.bus),
            self.vtube,
            self.send_message,
            self._complete_redemption,
            context=lambda p: (self.platforms[p].channel_name, self.platforms[p].stream_info),
        )

    @property
    def builtin_commands(self) -> list[tuple[str, Permission]]:
        return self._command_service.builtins()

    def builtin_enabled(self, name: str) -> bool:
        return self._command_service.is_enabled(name)

    def set_builtin_enabled(self, name: str, enabled: bool) -> None:
        self._command_service.set_enabled(name, enabled)

    def start(self) -> None:
        """Arranca los servicios en segundo plano (requiere el bucle asyncio en marcha)."""
        self._timer_service.start()
        self.tts.start()
        self.reactions.start()
        self.vtube.start()

    # ------------------------------------------------- VTube Studio y premios
    async def start_overlay(self) -> bool:
        return await self.overlay.start()

    async def set_overlay(self, enabled: bool, port: int | None = None) -> bool:
        """Activa/desactiva el overlay o cambia su puerto (lo reinicia). Devuelve si quedó escuchando."""
        self.config.overlay.enabled = enabled
        if port is not None:
            self.config.overlay.port = int(port)
        self._save_config()
        return await self.overlay.start()

    def save_overlay_style(self) -> None:
        """Guarda el estilo y lo aplica al momento en OBS y en la vista previa."""
        self.overlay.config_changed()
        self._save_config()

    async def set_vtube(self, enabled: bool, port: int | None = None) -> None:
        self.config.vtube.enabled = enabled
        if port:
            self.config.vtube.port = int(port)
        self._save_config()
        await self.vtube.restart()

    def _tts_may_read(self, msg: ChatMessage) -> bool:
        """Modo «todo el chat»: ni lo que bloquean los filtros ni los mensajes del propio bot."""
        client = self.platforms.get(msg.platform)
        if client is None or msg.user_id == client.bot_user_id:
            return False
        return not self.moderation.is_blocked(msg)

    async def _complete_redemption(self, platform: Platform, reward_id: str, redemption_id: str,
                                   refund: bool) -> None:
        await self.platforms[platform].complete_redemption(reward_id, redemption_id, refund)

    async def list_rewards(self) -> tuple[list[Reward], list[str]]:
        """Premios de las plataformas conectadas (marcando los creados por la app) y errores."""
        rewards, errors = [], []
        for p, client in self.platforms.items():
            if not client.is_connected:
                continue
            try:
                managed = self.reactions.repo.managed_ids(p)
                rewards += [replace(r, managed=r.id in managed) for r in await client.list_rewards()]
            except PlatformError as exc:
                errors.append(f"{p.label}: {exc}")
        return rewards, errors

    async def create_reward(self, platform: Platform, title: str, cost: int, prompt: str = "",
                            input_required: bool = False) -> Reward:
        client = self.platforms[platform]
        if input_required:  # premio del TTS: el viewer escribe el mensaje al canjearlo
            reward = await client.create_reward(title, cost, prompt, input_required=True)
        else:
            reward = await client.create_reward(title, cost, prompt)
        self.reactions.repo.add_managed_reward(platform, reward.id, reward.title)
        return reward

    async def delete_reward(self, platform: Platform, reward_id: str) -> None:
        await self.platforms[platform].delete_reward(reward_id)
        self.reactions.repo.remove_managed_reward(platform, reward_id)

    # ------------------------------------------------------------ conexión
    async def connect_all(self) -> list[str]:
        """Conecta todas las plataformas. Devuelve los errores (vacío si todo fue bien)."""
        clients = [c for c in self.platforms.values() if c.is_configured]
        if not clients:
            return ["Configura Twitch o Kick en Configuración para empezar."]
        errors = []
        for client in clients:
            try:
                await client.connect()
            except PlatformError as exc:
                errors.append(f"{client.platform.label}: {exc}")
        return errors

    async def disconnect_all(self) -> None:
        for client in self.platforms.values():
            if client.state is not ConnectionState.DISCONNECTED:
                await client.disconnect()

    async def connect_platform(self, platform: Platform) -> str | None:
        """Conecta una sola plataforma. Devuelve el error para mostrarlo, o ``None`` si fue bien."""
        client = self.platforms[platform]
        if not client.is_configured:
            return f"{platform.label}: configura la cuenta en Configuración primero."
        try:
            await client.connect()
        except PlatformError as exc:
            return f"{platform.label}: {exc}"
        return None

    def cached_avatar(self, platform: Platform) -> Path | None:
        """La foto de perfil ya descargada de esa plataforma, si la hay."""
        path = avatar_path(platform)
        return path if self._real_data and path.is_file() else None

    async def fetch_avatar(self, platform: Platform) -> Path | None:
        """Descarga la foto de perfil del streamer de esa plataforma (conectada) a ``avatars/``.

        Devuelve la ruta, o ``None`` si no hay foto o falló (la interfaz se queda con las iniciales).
        """
        client = self.platforms[platform]
        if not self._real_data or not client.is_connected:
            return None
        try:
            url = await client.profile_image_url()
            if not url or not url.startswith("https://"):
                return None
            data = await download_image(url)
        except Exception as exc:  # sin foto no pasa nada: se ven las iniciales
            log.info("%s: no se pudo descargar la foto de perfil (%s)", platform.label, type(exc).__name__)
            return None
        if not data or len(data) > AVATAR_MAX_BYTES:
            return None
        path = avatar_path(platform)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)  # escritura atómica
        return path

    async def disconnect_platform(self, platform: Platform) -> None:
        client = self.platforms[platform]
        if client.state is not ConnectionState.DISCONNECTED:
            await client.disconnect()

    @property
    def any_connected(self) -> bool:
        return any(c.is_connected for c in self.platforms.values())

    async def send_message(self, platform: Platform, text: str) -> None:
        await self.platforms[platform].send_message(text)

    # -------------------------------------------------------------- cuentas
    def account(self, platform: Platform, role: AccountRole) -> AccountInfo | None:
        module = twitch_auth if platform is Platform.TWITCH else kick_auth
        return module.load_account(role)

    async def authorize(self, platform: Platform, role: AccountRole,
                        on_code: Callable[[str, str], None] | None = None) -> AccountInfo:
        """Inicia sesión con una cuenta. En Twitch, ``on_code(código, url)`` recibe el código a confirmar."""
        if platform is Platform.TWITCH:
            info = await twitch_auth.authorize_account(role, on_code)
        else:
            info = await kick_auth.authorize_account(self.platforms[Platform.KICK].app(), role)
        self.bus.account_changed.emit(platform, role, info)
        return info

    def forget(self, platform: Platform, role: AccountRole) -> None:
        module = twitch_auth if platform is Platform.TWITCH else kick_auth
        module.forget_account(role)
        self.bus.account_changed.emit(platform, role, None)

    # ---------------------------------------------------------------- cierre
    async def shutdown(self) -> None:
        log.info("Cerrando AlpekaBOT…")
        await self._timer_service.stop()
        self.songs.stop()
        await self.tts.stop()
        await self.reactions.stop()
        await self.vtube.stop()
        try:
            await asyncio.wait_for(self.moderation.wait_idle(), 3)
        except asyncio.TimeoutError:
            log.warning("Moderación: sanciones pendientes sin terminar al cerrar")
        await self.disconnect_all()
        await self.overlay.stop()
        self.db.close()


def _overlay_token() -> str | None:
    """Token fijo de la URL del overlay (en keyring), para que la URL puesta en OBS no cambie."""
    try:
        token = credentials.get_secret(OVERLAY_TOKEN_SECRET)
        if not token:
            token = secrets.token_urlsafe(18)
            credentials.set_secret(OVERLAY_TOKEN_SECRET, token)
        return token
    except Exception:  # noqa: BLE001 (keyring no disponible: token de esta sesión)
        log.warning("No se pudo guardar el token del overlay: la URL de OBS cambiará al reiniciar")
        return None
