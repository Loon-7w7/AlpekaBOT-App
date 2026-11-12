"""Conexión a Kick: chat entrante por webhooks y salida por la API pública.

Los webhooks llegan por el intermediario de AlpekaBOT (app compartida, por defecto)
o por ngrok a un servidor local (app propia, avanzado).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from datetime import datetime, timezone

from ... import credentials
from ...config import KickConfig
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
from . import auth, relay
from .api import KickAPI, KickAPIError, KickUnauthorized
from .relay import RelayConnection
from .tunnel import NgrokTunnel, normalize_domain
from .webhook import WEBHOOK_PATH, SignatureVerifier, WebhookServer

log = logging.getLogger(__name__)

MAX_MESSAGE_LEN = 500
MAX_TIMEOUT_MIN = 10_080  # 7 días, máximo de Kick
STREAM_POLL_S = 60
EVENTS = [
    "chat.message.sent", "livestream.status.updated", "livestream.metadata.updated",
    # Fase 7: eventos para las reacciones de VTube Studio
    "channel.followed", "channel.subscription.new", "channel.subscription.renewal",
    "channel.subscription.gifts", "kicks.gifted", "channel.reward.redemption.updated",
]
SEEN_REDEMPTIONS = 500  # canjes recordados para no reaccionar dos veces (pendiente → aceptado)
_EMOTE_RE = re.compile(r"\[emote:\d+:([^\]]+)\]")
_BADGES = {"broadcaster", "moderator", "vip", "subscriber", "founder"}


def strip_emotes(text: str) -> str:
    """'hola [emote:37226:KEKW]' -> 'hola KEKW'."""
    return _EMOTE_RE.sub(r"\1", text)


def parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def chat_message_from_payload(p: dict) -> ChatMessage | None:
    sender = p.get("sender") or {}
    broadcaster = p.get("broadcaster") or {}
    if not sender or not p.get("message_id"):
        return None
    identity = sender.get("identity") or {}
    badges = {b.get("type", "") for b in identity.get("badges") or []} & _BADGES
    if sender.get("user_id") is not None and sender.get("user_id") == broadcaster.get("user_id"):
        badges.add("broadcaster")
    username = str(sender.get("username", ""))
    return ChatMessage(
        platform=Platform.KICK,
        message_id=str(p["message_id"]),
        channel_id=str(broadcaster.get("user_id", "")),
        user_id=str(sender.get("user_id", "")),
        user_login=username.lower(),
        user_name=username,
        text=strip_emotes(str(p.get("content", ""))),
        color=identity.get("username_color") or "",
        badges=tuple(sorted(badges)),
        reply_to_id=(p.get("replies_to") or {}).get("message_id"),
    )


def _user(obj) -> tuple[str, str]:
    """(user_id, nombre) de un objeto de usuario de Kick; anónimo si no hay datos."""
    obj = obj or {}
    if obj.get("is_anonymous") or obj.get("user_id") is None:
        return "", "Anónimo"
    return str(obj.get("user_id")), str(obj.get("username") or "")


def channel_event_from_payload(ev_type: str, p: dict) -> ChannelEvent | None:
    """Convierte un webhook de Kick en un ChannelEvent común (o None si no interesa)."""
    if ev_type == "channel.followed":
        return ChannelEvent(Platform.KICK, EventKind.FOLLOW, *_user(p.get("follower")))
    if ev_type in ("channel.subscription.new", "channel.subscription.renewal"):
        kind = EventKind.SUB if ev_type.endswith(".new") else EventKind.RESUB
        return ChannelEvent(Platform.KICK, kind, *_user(p.get("subscriber")), _int(p.get("duration"), 1))
    if ev_type == "channel.subscription.gifts":
        return ChannelEvent(Platform.KICK, EventKind.GIFT, *_user(p.get("gifter")), len(p.get("giftees") or []))
    if ev_type == "kicks.gifted":
        return ChannelEvent(Platform.KICK, EventKind.BITS, *_user(p.get("sender")),
                            _int((p.get("gift") or {}).get("amount")))
    if ev_type == "channel.reward.redemption.updated":
        status = str(p.get("status", "")).lower()
        reward = p.get("reward") or {}
        if status not in ("pending", "accepted") or not p.get("id") or not reward.get("id"):
            return None  # rechazado: no se reacciona
        return ChannelEvent(
            Platform.KICK, EventKind.REWARD, *_user(p.get("redeemer")), _int(reward.get("cost")),
            str(reward["id"]), str(reward.get("title", "")), str(p["id"]), str(p.get("user_input") or ""),
            pending=status == "pending",
        )
    return None


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class KickClient(PlatformClient):
    platform = Platform.KICK

    def __init__(self, config: KickConfig, bus: EventBus) -> None:
        super().__init__(bus)
        self._config = config
        self._api = KickAPI()
        self._tokens: dict[AccountRole, auth.TokenStore] = {}
        self._webhook: WebhookServer | None = None
        self._tunnel = NgrokTunnel()
        self._relay: RelayConnection | None = None
        self._sub_ids: list[str] = []
        self._poll_task: asyncio.Task | None = None
        self.bot: AccountInfo | None = None
        self.streamer: AccountInfo | None = None
        self._slug = ""
        self._seen_redemptions: OrderedDict[str, None] = OrderedDict()

    @property
    def is_configured(self) -> bool:
        if self._config.use_own_app:
            return bool(self._config.client_id)
        # App compartida: «configurado» = hay alguna cuenta conectada
        return any(auth.load_account(role) is not None for role in AccountRole)

    def app(self) -> auth.KickApp:
        cfg = self._config
        return auth.own_app(cfg.client_id, cfg.redirect_url) if cfg.use_own_app else auth.shared_app()

    @property
    def bot_user_id(self) -> str | None:
        return self.bot.user_id if self.bot else None

    @property
    def channel_name(self) -> str:
        return self.streamer.display_name if self.streamer else ""

    @property
    def webhook_url(self) -> str:
        domain = normalize_domain(self._config.ngrok_domain)
        return f"https://{domain}{WEBHOOK_PATH}" if domain else ""

    # ------------------------------------------------------------ llamadas
    async def _call(self, role: AccountRole, fn, *args, **kwargs):
        """Llama a la API con el token de la cuenta; si Kick dice 401, renueva y reintenta una vez."""
        store = self._tokens[role]
        try:
            return await fn(await store.access_token(), *args, **kwargs)
        except KickUnauthorized:
            await store.refresh()
            return await fn(await store.access_token(), *args, **kwargs)

    # ------------------------------------------------------------ conexión
    async def connect(self) -> None:
        if self._state in (ConnectionState.CONNECTED, ConnectionState.CONNECTING):
            return
        self._set_state(ConnectionState.CONNECTING)
        try:
            await self._connect()
        except asyncio.CancelledError:
            await self._cleanup()
            self._set_state(ConnectionState.DISCONNECTED)
            raise
        except PlatformError as exc:
            await self._cleanup()
            self._set_state(ConnectionState.ERROR, str(exc))
            raise
        except Exception as exc:
            log.exception("Error conectando a Kick")
            await self._cleanup()
            self._set_state(ConnectionState.ERROR, type(exc).__name__)
            raise PlatformError(f"No se pudo conectar a Kick ({type(exc).__name__}).") from None
        self._set_state(ConnectionState.CONNECTED, f"canal {self._slug or self.streamer.login} como {self.bot.display_name}")
        self._poll_task = asyncio.ensure_future(self._poll_stream())

    async def _connect(self) -> None:
        cfg = self._config
        kick_app = self.app()
        self._tokens = {r: auth.TokenStore(r, kick_app) for r in AccountRole}
        # Comprueba ambas sesiones (y renueva tokens si hace falta)
        bot_user = await self._call(AccountRole.BOT, self._api.current_user)
        streamer_user = await self._call(AccountRole.STREAMER, self._api.current_user)
        self.bot = self._tokens[AccountRole.BOT].info
        self.streamer = self._tokens[AccountRole.STREAMER].info
        if str(bot_user.get("user_id")) != self.bot.user_id or str(streamer_user.get("user_id")) != self.streamer.user_id:
            raise PlatformError("Los tokens de Kick no coinciden con las cuentas guardadas. Vuelve a conectarlas.")

        verifier = SignatureVerifier(self._api.public_key)
        await verifier.load()
        self._webhook = WebhookServer(verifier, self._on_event)
        if cfg.use_own_app:
            port = await self._webhook.start()
            domain = normalize_domain(cfg.ngrok_domain)
            await self._tunnel.start(credentials.get_secret(auth.NGROK_TOKEN_KEY) or "", domain, port)
        else:
            # Los eventos llegan por el intermediario y se verifican igual (firma de Kick)
            self._relay = RelayConnection(relay.RELAY_BASE, self._tokens[AccountRole.STREAMER],
                                          self._webhook.process, channel=self.streamer.login)
            await self._relay.start()

        await self._subscribe()
        await self._refresh_stream()

    async def _subscribe(self) -> None:
        sid = self.streamer.user_id
        existing = await self._call(AccountRole.STREAMER, self._api.list_subscriptions, sid)
        have = {s.get("event"): s.get("id") for s in existing if str(s.get("broadcaster_user_id")) == sid}
        missing = [e for e in EVENTS if e not in have]
        self._sub_ids = [i for e, i in have.items() if e in EVENTS and i]
        if missing:
            results = await self._call(AccountRole.STREAMER, self._api.create_subscriptions, missing)
            for r in results:
                if r.get("subscription_id"):
                    self._sub_ids.append(r["subscription_id"])
                elif r.get("error"):
                    log.warning("Kick no aceptó la suscripción a %s: %s", r.get("name"), str(r.get("error"))[:120])
        log.info("Kick: suscrito a %d eventos", len(self._sub_ids))

    async def disconnect(self) -> None:
        await self._cleanup()
        self._set_state(ConnectionState.DISCONNECTED)

    async def _cleanup(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
            self._poll_task = None
        if self._sub_ids and AccountRole.STREAMER in self._tokens:
            # Sin la app abierta Kick no podría entregar los eventos: se borran las suscripciones
            try:
                await self._call(AccountRole.STREAMER, self._api.delete_subscriptions, self._sub_ids)
            except Exception:
                log.info("No se pudieron borrar las suscripciones de Kick (se reutilizarán al conectar)")
            self._sub_ids = []
        await self._tunnel.close()
        if self._relay is not None:
            await self._relay.close()
            self._relay = None
        if self._webhook is not None:
            await self._webhook.close()
            self._webhook = None
        await self._api.close()
        self._set_stream(StreamInfo(Platform.KICK))

    # ----------------------------------------------------------- directo
    async def _poll_stream(self) -> None:
        while True:
            await asyncio.sleep(STREAM_POLL_S)
            try:
                await self._refresh_stream()
            except asyncio.CancelledError:
                raise
            except PlatformError as exc:
                log.warning("Kick: no se pudo consultar el estado del directo (%s)", exc)
            except Exception:
                log.exception("Kick: error consultando el estado del directo")

    async def _refresh_stream(self) -> None:
        ch = await self._call(AccountRole.STREAMER, self._api.channel, self.streamer.user_id)
        if not ch:
            return
        self._slug = ch.get("slug", self._slug)
        stream = ch.get("stream") or {}
        live = bool(stream.get("is_live"))
        self._set_stream(StreamInfo(
            Platform.KICK, live, ch.get("stream_title", ""), (ch.get("category") or {}).get("name", ""),
            parse_time(stream.get("start_time")) if live else None,
        ))

    # ------------------------------------------------------------ eventos
    def _on_event(self, ev_type: str, version: int, payload: dict) -> None:
        broadcaster = str((payload.get("broadcaster") or {}).get("user_id", ""))
        if self.streamer is None or broadcaster != self.streamer.user_id:
            return  # eventos de otros canales: se ignoran
        if ev_type == "chat.message.sent":
            msg = chat_message_from_payload(payload)
            if msg:
                self._bus.chat_message.emit(msg)
        elif ev_type == "livestream.status.updated":
            live = bool(payload.get("is_live"))
            cur = self.stream_info
            self._set_stream(StreamInfo(
                Platform.KICK, live, payload.get("title") or cur.title, cur.game,
                parse_time(payload.get("started_at")) if live else None,
            ))
        elif ev_type == "livestream.metadata.updated":
            meta = payload.get("metadata") or {}
            cur = self.stream_info
            self._set_stream(StreamInfo(
                Platform.KICK, cur.is_live, meta.get("title") or cur.title,
                (meta.get("category") or {}).get("name") or cur.game, cur.started_at,
            ))
        else:
            ev = channel_event_from_payload(ev_type, payload)
            if ev is None:
                return
            if ev.redemption_id:
                if ev.redemption_id in self._seen_redemptions:
                    return  # el mismo canje otra vez (p. ej. al aceptarlo nosotros)
                self._seen_redemptions[ev.redemption_id] = None
                while len(self._seen_redemptions) > SEEN_REDEMPTIONS:
                    self._seen_redemptions.popitem(last=False)
            self._bus.channel_event.emit(ev)

    # ------------------------------------------------ premios (fase 7)
    async def _rewards(self, what: str, fn, *args):
        self._channel_id()
        try:
            return await self._call(AccountRole.STREAMER, fn, *args)
        except KickAPIError as exc:
            hint = " Reconecta la cuenta del streamer de Kick." if exc.status in (401, 403) else ""
            raise PlatformError(f"Kick no dejó {what}: {exc}{hint}") from None

    async def list_rewards(self) -> list[Reward]:
        data = await self._rewards("leer los premios", self._api.list_rewards)
        return [Reward(Platform.KICK, str(r["id"]), str(r.get("title", "")), _int(r.get("cost")))
                for r in data if isinstance(r, dict) and r.get("id")]

    async def create_reward(self, title: str, cost: int, prompt: str = "") -> Reward:
        r = await self._rewards("crear el premio", self._api.create_reward, title, cost, prompt)
        if not r.get("id"):
            raise PlatformError("Kick no devolvió el premio creado.")
        return Reward(Platform.KICK, str(r["id"]), str(r.get("title", title)), _int(r.get("cost"), cost), True)

    async def delete_reward(self, reward_id: str) -> None:
        await self._rewards("borrar el premio", self._api.delete_reward, reward_id)

    async def complete_redemption(self, reward_id: str, redemption_id: str, refund: bool) -> None:
        failed = await self._rewards("actualizar el canje", self._api.decide_redemptions,
                                     [redemption_id], not refund)
        if failed:
            raise PlatformError(f"Kick no aceptó el cambio del canje ({failed[0].get('reason', 'UNKNOWN')})")

    # --------------------------------------------------------------- chat
    async def send_message(self, text: str, reply_to_id: str | None = None) -> None:
        text = text.strip()
        if not text:
            return
        if not self.is_connected or self.streamer is None:
            raise PlatformError("Kick no está conectado")
        if len(text) > MAX_MESSAGE_LEN:
            raise PlatformError(f"El mensaje supera {MAX_MESSAGE_LEN} caracteres")
        try:
            data = await self._call(AccountRole.BOT, self._api.send_chat, self.streamer.user_id, text, reply_to_id)
        except KickAPIError as exc:
            raise PlatformError(f"Kick rechazó el mensaje: {exc}") from None
        if data and data.get("is_sent") is False:
            raise PlatformError("Kick descartó el mensaje.")

    # --------------------------------------------------------- moderación
    # Modera la cuenta del streamer (tiene moderation:ban y moderation:chat_message:manage).
    def _channel_id(self) -> str:
        if not self.is_connected or self.streamer is None:
            raise PlatformError("Kick no está conectado")
        return self.streamer.user_id

    async def _moderate(self, what: str, fn, *args) -> None:
        try:
            await self._call(AccountRole.STREAMER, fn, *args)
        except KickAPIError as exc:
            hint = " Reconecta la cuenta del streamer de Kick." if exc.status in (401, 403) else ""
            raise PlatformError(f"Kick no dejó {what}: {exc}{hint}") from None

    async def delete_message(self, message_id: str) -> None:
        self._channel_id()
        await self._moderate("borrar el mensaje", self._api.delete_chat_message, message_id)

    async def timeout_user(self, user_id: str, seconds: int, reason: str = "") -> None:
        # Kick usa minutos: se redondea hacia arriba
        minutes = max(1, min(-(-int(seconds) // 60), MAX_TIMEOUT_MIN))
        await self._moderate("aplicar el timeout", self._api.ban, self._channel_id(), user_id, minutes, reason)

    async def ban_user(self, user_id: str, reason: str = "") -> None:
        await self._moderate("banear", self._api.ban, self._channel_id(), user_id, None, reason)

    async def unban_user(self, user_id: str) -> None:
        await self._moderate("desbanear", self._api.unban, self._channel_id(), user_id)

    async def lookup_user(self, login: str) -> tuple[str, str] | None:
        """Kick no busca usuarios por nombre: se usa su canal (slug = nombre con '_' cambiado por '-')."""
        self._channel_id()
        login = login.strip().lower()
        for slug in dict.fromkeys((login.replace("_", "-"), login)):
            try:
                ch = await self._call(AccountRole.STREAMER, self._api.channel_by_slug, slug)
            except KickAPIError as exc:
                if exc.status in (400, 404):
                    continue
                raise PlatformError(f"Kick no pudo buscar a ese usuario: {exc}") from None
            if ch and ch.get("broadcaster_user_id") is not None:
                return str(ch["broadcaster_user_id"]), login
        return None
