"""Modal «Conexiones» (botón de apagado): conectar y desconectar cada plataforma por separado."""

import asyncio


from alpekabot.core.event_bus import EventBus
from alpekabot.core.models import ConnectionState, Platform
from alpekabot.ui.connections_dialog import ConnectionsDialog


class Client:
    def __init__(self, platform, state=ConnectionState.CONNECTED, configured=True):
        self.platform, self.state, self.is_configured = platform, state, configured


class Bot:
    def __init__(self):
        self.bus = EventBus()
        self.platforms = {Platform.TWITCH: Client(Platform.TWITCH), Platform.KICK: Client(Platform.KICK)}
        self.calls = []

    async def connect_platform(self, platform):
        self.calls.append(("connect", platform))
        self.platforms[platform].state = ConnectionState.CONNECTED
        return None

    async def disconnect_platform(self, platform):
        self.calls.append(("disconnect", platform))
        self.platforms[platform].state = ConnectionState.DISCONNECTED


def test_cada_plataforma_se_desconecta_por_separado(qt_app):
    bot = Bot()
    dlg = ConnectionsDialog(bot)
    rows = dlg._rows
    assert {r.button.text() for r in rows.values()} == {"Desconectar"}

    async def run():
        dlg.toggle(Platform.KICK)
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert bot.calls == [("disconnect", Platform.KICK)]  # Twitch sigue conectada
    assert rows[Platform.KICK].button.text() == "Conectar"
    assert rows[Platform.TWITCH].button.text() == "Desconectar"


def test_sin_cuenta_no_se_puede_conectar(qt_app):
    bot = Bot()
    bot.platforms[Platform.KICK] = Client(Platform.KICK, ConnectionState.DISCONNECTED, configured=False)
    dlg = ConnectionsDialog(bot)
    row = dlg._rows[Platform.KICK]
    assert row.button.text() == "Conectar" and not row.button.isEnabled()
