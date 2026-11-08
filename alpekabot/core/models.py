"""Modelos comunes a todas las plataformas (Twitch, Kick...).

El resto del bot (comandos, moderación, estadísticas) trabaja solo con estos
tipos, así añadir una plataforma nueva no obliga a tocar esa lógica.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, IntEnum


class Platform(str, Enum):
    TWITCH = "twitch"
    KICK = "kick"

    @property
    def label(self) -> str:
        return {"twitch": "Twitch", "kick": "Kick"}[self.value]


class AccountRole(str, Enum):
    BOT = "bot"
    STREAMER = "streamer"

    @property
    def label(self) -> str:
        return {"bot": "Bot", "streamer": "Streamer"}[self.value]


class ConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"

    @property
    def label(self) -> str:
        return {
            "disconnected": "Desconectado",
            "connecting": "Conectando…",
            "connected": "Conectado",
            "error": "Error",
        }[self.value]


@dataclass(frozen=True)
class AccountInfo:
    platform: Platform
    role: AccountRole
    user_id: str
    login: str
    display_name: str


@dataclass(frozen=True)
class ChatMessage:
    platform: Platform
    message_id: str
    channel_id: str
    user_id: str
    user_login: str
    user_name: str
    text: str
    color: str = ""
    badges: tuple[str, ...] = ()
    reply_to_id: str | None = None
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def is_broadcaster(self) -> bool:
        return "broadcaster" in self.badges

    @property
    def is_moderator(self) -> bool:
        return "moderator" in self.badges or self.is_broadcaster

    @property
    def permission(self) -> "Permission":
        return Permission.of_badges(self.badges)


class Permission(IntEnum):
    """Niveles de permiso, de menor a mayor. Un comando exige un mínimo."""

    EVERYONE = 0
    SUBSCRIBER = 1
    VIP = 2
    MODERATOR = 3
    BROADCASTER = 4

    @property
    def label(self) -> str:
        return ("Todos", "Suscriptores", "VIP", "Moderadores", "Streamer")[self.value]

    @classmethod
    def of_badges(cls, badges: tuple[str, ...]) -> "Permission":
        b = set(badges)
        if "broadcaster" in b:
            return cls.BROADCASTER
        if "moderator" in b:
            return cls.MODERATOR
        if "vip" in b:
            return cls.VIP
        if b & {"subscriber", "founder"}:
            return cls.SUBSCRIBER
        return cls.EVERYONE


ALL_PLATFORMS: frozenset[Platform] = frozenset(Platform)


def platforms_to_str(platforms: frozenset[Platform]) -> str:
    return ",".join(sorted(p.value for p in platforms))


def platforms_from_str(raw: str) -> frozenset[Platform]:
    return frozenset(Platform(v) for v in raw.split(",") if v in {p.value for p in Platform})


class EventKind(str, Enum):
    """Eventos del canal que pueden disparar una reacción (fase 7)."""

    REWARD = "reward"   # canje de puntos de canal / premio
    FOLLOW = "follow"
    SUB = "sub"         # suscripción nueva
    RESUB = "resub"     # renovación
    GIFT = "gift"       # subs regaladas (amount = cuántas)
    BITS = "bits"       # bits de Twitch / kicks de Kick (amount = cantidad)
    RAID = "raid"       # solo Twitch (amount = espectadores)

    @property
    def label(self) -> str:
        return {
            "reward": "Canje de premio", "follow": "Follow", "sub": "Sub nueva", "resub": "Renovación de sub",
            "gift": "Subs regaladas", "bits": "Bits / Kicks", "raid": "Raid",
        }[self.value]


@dataclass(frozen=True)
class ChannelEvent:
    platform: Platform
    kind: EventKind
    user_id: str = ""
    user_name: str = ""
    amount: int = 0               # bits/kicks, subs regaladas, espectadores del raid, meses...
    reward_id: str = ""           # solo canjes
    reward_title: str = ""
    redemption_id: str = ""
    user_input: str = ""
    pending: bool = False         # canje pendiente de completar (se puede aceptar o devolver)


@dataclass(frozen=True)
class Reward:
    platform: Platform
    id: str
    title: str
    cost: int = 0
    managed: bool = False         # creado por la app: se puede devolver/borrar


@dataclass(frozen=True)
class StreamInfo:
    platform: Platform
    is_live: bool = False
    title: str = ""
    game: str = ""
    started_at: datetime | None = None  # con zona horaria (UTC)

    @property
    def uptime_text(self) -> str:
        if not self.is_live or self.started_at is None:
            return "offline"
        secs = max(0, int((datetime.now(timezone.utc) - self.started_at).total_seconds()))
        h, m = divmod(secs // 60, 60)
        return f"{h}h {m:02d}m" if h else f"{m}m"
