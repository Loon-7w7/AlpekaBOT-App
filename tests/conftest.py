"""Utilidades comunes para los tests: bot y plataforma falsos, base de datos en memoria."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # los tests de widgets no abren ventanas
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpekabot.core.event_bus import EventBus  # noqa: E402
from alpekabot.core.models import ChatMessage, Platform, StreamInfo  # noqa: E402
from alpekabot.storage.database import Database  # noqa: E402

BOT_ID = "bot-id"


@pytest.fixture(scope="session", autouse=True)
def qt_app():
    return QApplication.instance() or QApplication([])


@dataclass
class FakeClient:
    platform: Platform
    is_connected: bool = True
    bot_user_id: str = BOT_ID
    channel_name: str = "MiCanal"
    stream_info: StreamInfo = None

    def __post_init__(self):
        if self.stream_info is None:
            self.stream_info = StreamInfo(self.platform, is_live=True, title="Título", game="Juego")


@dataclass
class FakeBot:
    bus: EventBus = field(default_factory=EventBus)
    platforms: dict = field(default_factory=dict)
    sent: list = field(default_factory=list)

    async def send_message(self, platform, text):
        self.sent.append((platform, text))


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


@pytest.fixture
def bot():
    b = FakeBot()
    b.platforms[Platform.TWITCH] = FakeClient(Platform.TWITCH)
    return b


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def msg(text: str, user: str = "pepe", badges: tuple[str, ...] = (), user_id: str = "u1",
        platform: Platform = Platform.TWITCH) -> ChatMessage:
    return ChatMessage(platform, "m", "c", user_id, user.lower(), user, text, badges=badges)
