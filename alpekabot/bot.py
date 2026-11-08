"""Orquestador del bot: une configuración, plataformas, base de datos y funciones.

La interfaz habla solo con esta clase; nunca directamente con las plataformas.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from pathlib import Path

from . import credentials
from .config import AppConfig
from .core.event_bus import EventBus
from .core.models import AccountInfo, AccountRole, ConnectionState, Permission, Platform, Reward
from .features.commands import CommandRepository, CommandService
from .features.moderation import ModerationRepository, ModerationService
from .features.reactions import ReactionRepository, ReactionService
from .integrations.vtube_studio import VTubeStudioClient
from .features.songs import SongRepository, SongService
from .features.timers import TimerRepository, TimerService
from .features.tts import TtsService
from .features.updater import Updater
from .features.youtube import API_KEY_SECRET, YouTubeClient
from .paths import DB_FILE, NOW_PLAYING_FILE, ensure_dirs
from .platforms.base import PlatformClient, PlatformError
from .platforms.kick import auth as kick_auth
from .platforms.kick.client import KickClient
from .platforms.twitch import auth as twitch_auth
from .platforms.twitch.client import TwitchClient
from .storage.database import Database

log = logging.getLogger(__name__)


class AlpekaBot:
    def __init__(self, config: AppConfig, db_path: Path | str | None = None) -> None:
        self.config = config
        self.bus = EventBus()
        self.platforms: dict[Platform, PlatformClient] = {
            Platform.TWITCH: TwitchClient(config.twitch, self.bus),
            Platform.KICK: KickClient(config.kick, self.bus),
        }
        # db_path explícito = modo prueba: no se tocan data/config.json ni now_playing.txt
        real_data = db_path is None
        if real_data:
            ensure_dirs()
            db_path = DB_FILE
        self.db = Database(db_path)
        self.commands = CommandRepository(self.db, self.bus)
        self.timers = TimerRepository(self.db, self.bus)
        save_config = config.save if real_data else (lambda: None)
        # La moderación se crea ANTES que los comandos: revisa cada mensaje primero
        self.moderation = ModerationService(
            self, ModerationRepository(self.db, self.bus), config.moderation, save_config=save_config
        )
        self._command_service = CommandService(self, self.commands)
        self.moderation.register_commands(self._command_service)
        self._timer_service = TimerService(self, self.timers)
        self.songs = SongService(
            self.bus,
            SongRepository(self.db, self.bus),
            config.songs,
            YouTubeClient(lambda: credentials.get_secret(API_KEY_SECRET) or ""),
            save_config=save_config,
            now_playing_path=NOW_PLAYING_FILE if real_data else None,
            notify=self.send_message,
        )
        self.songs.register_commands(self._command_service)
        self.tts = TtsService(self.bus, config.tts, save_config=save_config)
        self.tts.register_commands(self._command_service)
        self._save_config = save_config
        self.updater = Updater()

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

    def start(self) -> None:
        """Arranca los servicios en segundo plano (requiere el bucle asyncio en marcha)."""
        self._timer_service.start()
        self.tts.start()
        self.reactions.start()
        self.vtube.start()

    # ------------------------------------------------- VTube Studio y premios
    async def set_vtube(self, enabled: bool, port: int | None = None) -> None:
        self.config.vtube.enabled = enabled
        if port:
            self.config.vtube.port = int(port)
        self._save_config()
        await self.vtube.restart()

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

    async def create_reward(self, platform: Platform, title: str, cost: int, prompt: str = "") -> Reward:
        reward = await self.platforms[platform].create_reward(title, cost, prompt)
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

    @property
    def any_connected(self) -> bool:
        return any(c.is_connected for c in self.platforms.values())

    async def send_message(self, platform: Platform, text: str) -> None:
        await self.platforms[platform].send_message(text)

    # -------------------------------------------------------------- cuentas
    def account(self, platform: Platform, role: AccountRole) -> AccountInfo | None:
        module = twitch_auth if platform is Platform.TWITCH else kick_auth
        return module.load_account(role)

    async def authorize(self, platform: Platform, role: AccountRole) -> AccountInfo:
        if platform is Platform.TWITCH:
            tw = self.config.twitch
            info = await twitch_auth.authorize_account(
                tw.client_id, twitch_auth.get_client_secret(), tw.redirect_url, role
            )
        else:
            kc = self.config.kick
            info = await kick_auth.authorize_account(
                kc.client_id, kick_auth.get_client_secret(), kc.redirect_url, role
            )
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
        self.db.close()
