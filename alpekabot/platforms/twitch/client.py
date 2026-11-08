"""Conexión a Twitch: chat entrante por EventSub WebSocket y salida por la API Helix."""

from __future__ import annotations

import asyncio
import logging

from twitchAPI.eventsub.websocket import EventSubWebsocket
from twitchAPI.helper import first
from twitchAPI.object.eventsub import (
    ChannelChatMessageEvent,
    ChannelCheerEvent,
    ChannelFollowEvent,
    ChannelPointsCustomRewardRedemptionAddEvent,
    ChannelRaidEvent,
    ChannelSubscribeEvent,
    ChannelSubscriptionGiftEvent,
    ChannelSubscriptionMessageEvent,
)
from twitchAPI.twitch import Twitch
from twitchAPI.type import CustomRewardRedemptionStatus, TwitchAPIException

from ...config import TwitchConfig
from ...core.event_bus import EventBus
from ...core.models import (
    AccountInfo,
    AccountRole,
    ChannelEvent,
    ChatMessage,
    ConnectionState,
    EventKind,
    Platform,
    Reward,
    StreamInfo,
)
from ..base import PlatformClient, PlatformError
from . import auth

log = logging.getLogger(__name__)

MAX_MESSAGE_LEN = 500  # límite de Twitch por mensaje
MAX_TIMEOUT_S = 1_209_600  # 14 días, máximo de Twitch
MAX_REASON_LEN = 500
STREAM_POLL_S = 60  # cada cuánto se consulta si el canal está en directo


class TwitchClient(PlatformClient):
    platform = Platform.TWITCH

    def __init__(self, config: TwitchConfig, bus: EventBus) -> None:
        super().__init__(bus)
        self._config = config
        self._bot_api: Twitch | None = None
        self._streamer_api: Twitch | None = None
        self._eventsub: EventSubWebsocket | None = None
        self._streamer_eventsub: EventSubWebsocket | None = None  # canjes y eventos (token del streamer)
        self._qt_loop: asyncio.AbstractEventLoop | None = None
        self._monitor_task: asyncio.Task | None = None
        self.bot: AccountInfo | None = None
        self.streamer: AccountInfo | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self._config.client_id)

    @property
    def bot_user_id(self) -> str | None:
        return self.bot.user_id if self.bot else None

    @property
    def channel_name(self) -> str:
        return self.streamer.display_name if self.streamer else ""

    # -------------------------------------------------------------- conexión
    async def connect(self) -> None:
        if self._state in (ConnectionState.CONNECTED, ConnectionState.CONNECTING):
            return
        self._set_state(ConnectionState.CONNECTING)
        try:
            secret = auth.get_client_secret()
            cid = self._config.client_id
            self._bot_api, self.bot = await auth.open_session(cid, secret, AccountRole.BOT)
            self._streamer_api, self.streamer = await auth.open_session(cid, secret, AccountRole.STREAMER)

            # NO pasar callback_loop: twitchAPI llamaría a create_task() sobre el bucle de Qt
            # desde su propio hilo, y eso no es seguro (AssertionError en qasync, mensajes
            # perdidos). Los callbacks corren en el hilo de EventSub y _on_chat_message
            # salta al hilo de Qt con call_soon_threadsafe.
            self._qt_loop = asyncio.get_running_loop()
            self._eventsub = EventSubWebsocket(self._bot_api)
            # start() bloquea hasta conectar: se ejecuta fuera del hilo de la interfaz
            await self._qt_loop.run_in_executor(None, self._eventsub.start)
            await self._eventsub.listen_channel_chat_message(
                broadcaster_user_id=self.streamer.user_id,
                user_id=self.bot.user_id,
                callback=self._on_chat_message,
            )
            await self._listen_channel_events()
        except asyncio.CancelledError:
            await self._cleanup()
            self._set_state(ConnectionState.DISCONNECTED)
            raise
        except PlatformError as exc:
            await self._cleanup()
            self._set_state(ConnectionState.ERROR, str(exc))
            raise
        except TwitchAPIException as exc:
            # Sin traceback ni texto: twitchAPI puede incluir URLs con el client_secret
            name = type(exc).__name__
            log.error("Twitch devolvió un error al conectar (%s)", name)
            await self._cleanup()
            self._set_state(ConnectionState.ERROR, name)
            raise PlatformError(f"Twitch devolvió un error al conectar ({name})") from None
        except Exception as exc:
            log.exception("Error conectando a Twitch")
            await self._cleanup()
            self._set_state(ConnectionState.ERROR, str(exc))
            raise PlatformError(f"No se pudo conectar a Twitch: {exc}") from exc

        self._set_state(
            ConnectionState.CONNECTED,
            f"canal #{self.streamer.login} como {self.bot.display_name}",
        )
        self._monitor_task = asyncio.ensure_future(self._monitor_stream())

    async def disconnect(self) -> None:
        await self._cleanup()
        self._set_state(ConnectionState.DISCONNECTED)

    # ------------------------------------------------------ estado del directo
    async def _monitor_stream(self) -> None:
        while True:
            try:
                await self._poll_stream()
            except asyncio.CancelledError:
                raise
            except TwitchAPIException as exc:
                log.warning("No se pudo consultar el estado del directo (%s)", type(exc).__name__)
            except Exception:
                log.exception("Error consultando el estado del directo")
            await asyncio.sleep(STREAM_POLL_S)

    async def _poll_stream(self) -> None:
        api, streamer = self._streamer_api, self.streamer
        if api is None or streamer is None:
            return
        stream = await first(api.get_streams(user_id=[streamer.user_id]))
        if stream is not None:
            info = StreamInfo(Platform.TWITCH, True, stream.title, stream.game_name, stream.started_at)
        else:
            channels = await api.get_channel_information(streamer.user_id)
            ch = channels[0] if channels else None
            info = StreamInfo(Platform.TWITCH, False, ch.title if ch else "", ch.game_name if ch else "")
        self._set_stream(info)

    async def _cleanup(self) -> None:
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except (asyncio.CancelledError, Exception):
                pass
            self._monitor_task = None
        self._set_stream(StreamInfo(Platform.TWITCH))
        for name in ("_eventsub", "_streamer_eventsub"):
            es = getattr(self, name)
            if es is not None:
                try:
                    await es.stop()
                except Exception:
                    log.debug("EventSub ya estaba detenido", exc_info=True)
                setattr(self, name, None)
        for api in (self._bot_api, self._streamer_api):
            if api is not None:
                try:
                    await api.close()
                except Exception:
                    log.debug("Error cerrando cliente Twitch", exc_info=True)
        self._bot_api = self._streamer_api = None

    # ----------------------------------------------------------------- chat
    async def send_message(self, text: str, reply_to_id: str | None = None) -> None:
        text = text.strip()
        if not text:
            return
        if not self.is_connected or self._bot_api is None:
            raise PlatformError("Twitch no está conectado")
        if len(text) > MAX_MESSAGE_LEN:
            raise PlatformError(f"El mensaje supera {MAX_MESSAGE_LEN} caracteres")
        try:
            resp = await self._bot_api.send_chat_message(
                self.streamer.user_id, self.bot.user_id, text, reply_parent_message_id=reply_to_id
            )
        except TwitchAPIException as exc:
            # Sin el texto de la excepción: twitchAPI puede incluir URLs con el client_secret
            raise PlatformError(f"Twitch rechazó el mensaje ({type(exc).__name__})") from None
        if not resp.is_sent:
            reason = resp.drop_reason.message if resp.drop_reason else "motivo desconocido"
            raise PlatformError(f"Twitch descartó el mensaje: {reason}")

    # ------------------------------------------------------------ moderación
    def _mod_context(self) -> tuple[Twitch, str, str]:
        """(cliente del bot, id del canal, id del moderador = el bot)."""
        if not self.is_connected or self._bot_api is None or self.bot is None or self.streamer is None:
            raise PlatformError("Twitch no está conectado")
        return self._bot_api, self.streamer.user_id, self.bot.user_id

    async def _moderate(self, what: str, call) -> None:
        try:
            await call
        except TwitchAPIException as exc:
            name = type(exc).__name__
            log.warning("Twitch rechazó %s (%s)", what, name)
            bot = self.bot.login if self.bot else "el bot"
            raise PlatformError(
                f"Twitch no dejó {what} ({name}). Comprueba que {bot} es moderador del canal "
                f"(escribe /mod {bot} en tu chat)."
            ) from None

    async def delete_message(self, message_id: str) -> None:
        api, channel, mod = self._mod_context()
        await self._moderate("borrar el mensaje", api.delete_chat_message(channel, mod, message_id))

    async def timeout_user(self, user_id: str, seconds: int, reason: str = "") -> None:
        api, channel, mod = self._mod_context()
        seconds = max(1, min(int(seconds), MAX_TIMEOUT_S))
        await self._moderate(
            "aplicar el timeout", api.ban_user(channel, mod, user_id, reason[:MAX_REASON_LEN], seconds)
        )

    async def ban_user(self, user_id: str, reason: str = "") -> None:
        api, channel, mod = self._mod_context()
        await self._moderate("banear", api.ban_user(channel, mod, user_id, reason[:MAX_REASON_LEN]))

    async def unban_user(self, user_id: str) -> None:
        api, channel, mod = self._mod_context()
        await self._moderate("desbanear", api.unban_user(channel, mod, user_id))

    async def lookup_user(self, login: str) -> tuple[str, str] | None:
        api, _, _ = self._mod_context()
        try:
            user = await first(api.get_users(logins=[login]))
        except TwitchAPIException as exc:
            log.warning("Twitch: no se pudo buscar al usuario (%s)", type(exc).__name__)
            raise PlatformError("Twitch no pudo buscar a ese usuario") from None
        return (user.id, user.display_name) if user else None

    # ----------------------------------------------- premios (fase 7)
    def _streamer_context(self) -> tuple[Twitch, str]:
        if not self.is_connected or self._streamer_api is None or self.streamer is None:
            raise PlatformError("Twitch no está conectado")
        return self._streamer_api, self.streamer.user_id

    async def _rewards_call(self, what: str, call):
        try:
            return await call
        except TwitchAPIException as exc:
            name = type(exc).__name__
            log.warning("Twitch rechazó %s (%s)", what, name)
            raise PlatformError(
                f"Twitch no dejó {what} ({name}). Los puntos de canal requieren ser afiliado o partner, "
                "y la cuenta del streamer debe estar reconectada con los permisos nuevos."
            ) from None

    async def list_rewards(self) -> list[Reward]:
        api, channel = self._streamer_context()
        rewards = await self._rewards_call("leer los premios", api.get_custom_reward(channel))
        return [Reward(Platform.TWITCH, r.id, r.title, r.cost) for r in rewards]

    async def create_reward(self, title: str, cost: int, prompt: str = "") -> Reward:
        api, channel = self._streamer_context()
        r = await self._rewards_call("crear el premio",
                                     api.create_custom_reward(channel, title[:45], max(1, int(cost)), prompt or None))
        return Reward(Platform.TWITCH, r.id, r.title, r.cost, managed=True)

    async def delete_reward(self, reward_id: str) -> None:
        api, channel = self._streamer_context()
        await self._rewards_call("borrar el premio", api.delete_custom_reward(channel, reward_id))

    async def complete_redemption(self, reward_id: str, redemption_id: str, refund: bool) -> None:
        api, channel = self._streamer_context()
        status = CustomRewardRedemptionStatus.CANCELED if refund else CustomRewardRedemptionStatus.FULFILLED
        await self._rewards_call("actualizar el canje",
                                 api.update_redemption_status(channel, reward_id, redemption_id, status))

    # ------------------------------------------- eventos del canal (fase 7)
    async def _listen_channel_events(self) -> None:
        """Segundo EventSub con el token del STREAMER: canjes, follows, subs, bits y raids.

        Cada suscripción va por separado: si una falla (p. ej. puntos de canal sin ser
        afiliado) las demás siguen funcionando.
        """
        api, sid = self._streamer_api, self.streamer.user_id
        self._streamer_eventsub = EventSubWebsocket(api)
        await self._qt_loop.run_in_executor(None, self._streamer_eventsub.start)
        es = self._streamer_eventsub
        subs = [
            ("canjes de puntos", es.listen_channel_points_custom_reward_redemption_add(sid, self._on_redemption)),
            ("follows", es.listen_channel_follow_v2(sid, sid, self._on_follow)),
            ("subs", es.listen_channel_subscribe(sid, self._on_subscribe)),
            ("renovaciones", es.listen_channel_subscription_message(sid, self._on_resub)),
            ("subs regaladas", es.listen_channel_subscription_gift(sid, self._on_gift)),
            ("bits", es.listen_channel_cheer(sid, self._on_cheer)),
            ("raids", es.listen_channel_raid(self._on_raid, to_broadcaster_user_id=sid)),
        ]
        failed = []
        for what, coro in subs:
            try:
                await coro
            except Exception as exc:  # solo el nombre: el texto de twitchAPI puede llevar secretos
                failed.append(f"{what} ({type(exc).__name__})")
        if failed:
            log.warning("Twitch: no se pudo escuchar %s. Si faltan permisos, reconecta la cuenta del streamer.",
                        ", ".join(failed))

    def _emit_event(self, ev: ChannelEvent) -> None:
        """Se llama en el hilo de EventSub: salta al hilo de Qt."""
        loop = self._qt_loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._bus.channel_event.emit, ev)

    async def _on_redemption(self, event: ChannelPointsCustomRewardRedemptionAddEvent) -> None:
        e = event.event
        self._emit_event(ChannelEvent(
            Platform.TWITCH, EventKind.REWARD, e.user_id, e.user_name, e.reward.cost, e.reward.id,
            e.reward.title, e.id, e.user_input or "", pending=str(e.status).lower() == "unfulfilled",
        ))

    async def _on_follow(self, event: ChannelFollowEvent) -> None:
        e = event.event
        self._emit_event(ChannelEvent(Platform.TWITCH, EventKind.FOLLOW, e.user_id, e.user_name))

    async def _on_subscribe(self, event: ChannelSubscribeEvent) -> None:
        e = event.event
        if e.is_gift:
            return  # quien recibe una sub regalada: ya cuenta en el evento del regalo
        self._emit_event(ChannelEvent(Platform.TWITCH, EventKind.SUB, e.user_id, e.user_name, 1))

    async def _on_resub(self, event: ChannelSubscriptionMessageEvent) -> None:
        e = event.event
        self._emit_event(ChannelEvent(Platform.TWITCH, EventKind.RESUB, e.user_id, e.user_name,
                                      int(e.cumulative_months or 0)))

    async def _on_gift(self, event: ChannelSubscriptionGiftEvent) -> None:
        e = event.event
        name = "Anónimo" if e.is_anonymous else (e.user_name or "")
        self._emit_event(ChannelEvent(Platform.TWITCH, EventKind.GIFT, e.user_id or "", name, int(e.total or 0)))

    async def _on_cheer(self, event: ChannelCheerEvent) -> None:
        e = event.event
        name = "Anónimo" if e.is_anonymous else (e.user_name or "")
        self._emit_event(ChannelEvent(Platform.TWITCH, EventKind.BITS, e.user_id or "", name, int(e.bits or 0)))

    async def _on_raid(self, event: ChannelRaidEvent) -> None:
        e = event.event
        self._emit_event(ChannelEvent(Platform.TWITCH, EventKind.RAID, e.from_broadcaster_user_id,
                                      e.from_broadcaster_user_name, int(e.viewers or 0)))

    async def _on_chat_message(self, event: ChannelChatMessageEvent) -> None:
        """Se ejecuta en el hilo de EventSub, NO en el de Qt."""
        e = event.event
        msg = ChatMessage(
            platform=Platform.TWITCH,
            message_id=e.message_id,
            channel_id=e.broadcaster_user_id,
            user_id=e.chatter_user_id,
            user_login=e.chatter_user_login,
            user_name=e.chatter_user_name,
            text=e.message.text,
            color=e.color or "",
            badges=tuple(b.set_id for b in (e.badges or [])),
            reply_to_id=e.reply.parent_message_id if e.reply else None,
        )
        loop = self._qt_loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._bus.chat_message.emit, msg)
