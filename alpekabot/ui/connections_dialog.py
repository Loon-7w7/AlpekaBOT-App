"""Modal «Conexiones»: conectar o desconectar cada plataforma por separado."""

from __future__ import annotations

import asyncio
import logging

from ..bot import AlpekaBot
from ..core.models import ConnectionState, Platform
from . import theme as t
from .widgets import Dot, ModalDialog, Pill, button, frame, hbox, label

log = logging.getLogger(__name__)

_STATES = {  # estado -> (texto, color)
    ConnectionState.DISCONNECTED: ("DESCONECTADO", t.TEXT_3),
    ConnectionState.CONNECTING: ("CONECTANDO", t.AMBER),
    ConnectionState.CONNECTED: ("CONECTADO", t.GREEN),
    ConnectionState.ERROR: ("ERROR", t.RED),
}
_OFF = (ConnectionState.DISCONNECTED, ConnectionState.ERROR)


class _Row:
    """Una plataforma: su estado y el botón que la conecta o la desconecta."""

    def __init__(self, dialog: "ConnectionsDialog", platform: Platform) -> None:
        self.platform = platform
        self.busy = False
        self.dot = Dot(t.PLATFORM_COLORS[platform.value], 10)
        self.pill = Pill("", t.TEXT_3, size=10)
        self.button = button("", slot=lambda: dialog.toggle(platform))
        self.button.setMinimumWidth(130)
        self.card = frame("cardInner", hbox(
            self.dot, label(platform.label, size=15, bold=True), self.pill, None, self.button,
            spacing=12, margins=(16, 12, 14, 12)))


class ConnectionsDialog(ModalDialog):
    def __init__(self, bot: AlpekaBot, parent=None) -> None:
        super().__init__("Conexiones", "Conecta o desconecta cada plataforma por separado.", "power", parent,
                         width=540)
        self._bot = bot
        self._rows = {p: _Row(self, p) for p in bot.platforms}
        for row in self._rows.values():
            self.body.addWidget(row.card)
        self._msg = label("", color=t.RED, size=12, wrap=True)
        self.body.addWidget(self._msg)
        self.footer.addStretch(1)
        self.footer.addWidget(button("Cerrar", slot=self.accept))
        bot.bus.connection_state.connect(self._refresh)
        self._refresh()

    def toggle(self, platform: Platform) -> None:
        if not self._rows[platform].busy:
            asyncio.ensure_future(self._toggle(platform))

    async def _toggle(self, platform: Platform) -> None:
        row = self._rows[platform]
        row.busy = True
        self._msg.setText("")
        self._refresh()
        try:
            if self._bot.platforms[platform].state in _OFF:
                error = await self._bot.connect_platform(platform)
                if error:
                    self._msg.setText(error)
            else:
                await self._bot.disconnect_platform(platform)
        except Exception:
            log.exception("Error al cambiar la conexión de %s", platform.label)
            self._msg.setText(f"{platform.label}: error inesperado (mira la consola).")
        finally:
            row.busy = False
            self._refresh()

    def _refresh(self, *_args) -> None:
        for platform, row in self._rows.items():
            client = self._bot.platforms[platform]
            state = client.state
            text, color = _STATES[state]
            row.pill.set(text, color)
            row.dot.set_color(t.PLATFORM_COLORS[platform.value] if state is ConnectionState.CONNECTED else t.TEXT_4)
            on = state not in _OFF
            row.button.setText("Desconectar" if on else "Conectar")
            row.button.setIcon(t.icon("unplug" if on else "plug", t.TEXT_2, 16))
            row.button.setEnabled(not row.busy and state is not ConnectionState.CONNECTING
                                  and (on or client.is_configured))
            row.button.setToolTip("" if client.is_configured else "Configura la cuenta en Configuración primero.")
