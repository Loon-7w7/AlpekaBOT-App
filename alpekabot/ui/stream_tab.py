"""Página «Directo»: ver y cambiar el título y la categoría en Twitch y Kick.

Un título y una categoría comunes y se elige en qué plataformas aplicarlos. La
categoría se busca mientras se escribe (en la primera plataforma elegida) y al
aplicarla cada plataforma usa su coincidencia exacta o, si no, la más parecida.
"""

from __future__ import annotations

import asyncio
import logging

from PySide6.QtCore import QStringListModel, Qt, QTimer
from PySide6.QtWidgets import QCompleter, QFrame, QLabel, QLineEdit, QWidget

from ..core.models import ConnectionState, Platform, StreamInfo
from ..features.stream_control import category_text
from ..platforms.base import PlatformError
from ..platforms.twitch.client import MAX_TITLE_LEN
from . import theme as t
from .commands_tab import scroll_page
from .widgets import Pill, PlatformPicker, button, frame, hbox, icon_label, label, vbox

log = logging.getLogger(__name__)

SEARCH_DELAY_MS = 400


class _Elided(QLabel):
    """Una línea recortada con «…»; ``set_full`` cambia el texto (ElidedLabel es solo de texto fijo)."""

    def __init__(self, css: str) -> None:
        super().__init__()
        self._full = ""
        self.setTextFormat(Qt.PlainText)  # títulos ajenos: nunca como HTML
        self.setStyleSheet(css)
        self.setMinimumWidth(80)

    def set_full(self, text: str) -> None:
        self._full = text
        self.setToolTip(text)
        self._elide()

    def full_text(self) -> str:
        return self._full

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt)
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        self.setText(self.fontMetrics().elidedText(self._full, Qt.ElideRight, max(40, self.width())))


class PlatformStatus(QFrame):
    """Tarjeta pequeña con el estado actual de una plataforma."""

    def __init__(self, platform: Platform) -> None:
        super().__init__()
        self.setObjectName("cardInner")
        self.platform = platform
        color = t.PLATFORM_COLORS[platform.value]
        self._pill = Pill("", t.TEXT_3, size=10)
        self.title_label = _Elided("font-size: 14px; font-weight: 600;")
        self.game_label = _Elided(f"color: {t.TEXT_2}; font-size: 12px;")
        head = hbox(icon_label("tv" if platform is Platform.TWITCH else "zap", color, 16),
                    label(platform.label, size=13, bold=True), None, self._pill, spacing=8)
        self.setLayout(vbox(head, self.title_label, self.game_label, spacing=6, margins=(14, 12, 14, 12)))

    def show_info(self, connected: bool, info: StreamInfo) -> None:
        if not connected:
            self._pill.set("SIN CONECTAR", t.TEXT_3)
        elif info.is_live:
            self._pill.set(f"EN DIRECTO · {info.uptime_text}", t.RED)
        else:
            self._pill.set("OFFLINE", t.TEXT_2)
        self.title_label.set_full(info.title or ("—" if connected else "Conecta la plataforma para verlo"))
        self.game_label.set_full(f"Categoría: {info.game}" if info.game else "Sin categoría")


class StreamTab(QWidget):
    title = "Directo (título y categoría)"
    icon = "radio"

    def __init__(self, bot, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("page")
        self._bot = bot
        self._busy = False
        self._search_task: asyncio.Task | None = None
        self.actions = None

        self._status = {p: PlatformStatus(p) for p in Platform}
        status_card = frame("card", vbox(label("ESTADO ACTUAL", "sectionTitle"), *self._status.values(),
                                         spacing=12, margins=(20, 16, 20, 18)))

        self._picker = PlatformPicker(frozenset(bot.stream.connected() or Platform))

        self._title_edit = QLineEdit()
        self._title_edit.setPlaceholderText("Título del directo")
        self._title_edit.setMaxLength(MAX_TITLE_LEN)
        self._title_count = label("", color=t.TEXT_3, size=11, mono=True)
        self._title_edit.textChanged.connect(self._update_count)

        self._game_edit = QLineEdit()
        self._game_edit.setPlaceholderText("Busca una categoría (p. ej. Just Chatting, Minecraft)")
        self._game_model = QStringListModel(self)
        completer = QCompleter(self._game_model, self)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        self._game_edit.setCompleter(completer)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(SEARCH_DELAY_MS)
        self._search_timer.timeout.connect(self._start_search)
        self._game_edit.textEdited.connect(lambda _t: self._search_timer.start())
        self._search_hint = label("", color=t.TEXT_3, size=11)

        self._apply_title = button("Aplicar título", "check", "primary", self._on_apply_title)
        self._apply_game = button("Aplicar categoría", "check", "primary", self._on_apply_game)
        current = button("Usar lo actual", "refresh-cw", slot=self._fill_current,
                         tooltip="Copia el título y la categoría actuales en los campos")
        self._result = label("", size=12, wrap=True)
        self._result.setTextFormat(Qt.PlainText)

        edit_card = frame("cardHi", vbox(
            hbox(label("CAMBIAR", "sectionTitle"), None, current),
            label("Aplicar en", color=t.TEXT_2, size=12), self._picker,
            label("Título", color=t.TEXT_2, size=12),
            hbox(self._title_edit, self._title_count, self._apply_title, spacing=10),
            label("Categoría", color=t.TEXT_2, size=12),
            hbox(self._game_edit, self._apply_game, spacing=10),
            self._search_hint,
            self._result,
            label("En el chat: !title y !game (los mods pueden cambiarlos; «!title twitch …» cambia solo una "
                  "plataforma).", color=t.TEXT_3, size=11, wrap=True),
            spacing=10, margins=(22, 18, 22, 20)))

        right = QWidget()
        right.setFixedWidth(420)
        right.setLayout(vbox(status_card, None))
        left = QWidget()
        left.setLayout(vbox(edit_card, None))
        columns = hbox(left, right, spacing=22)
        columns.setStretchFactor(left, 1)
        self.setLayout(vbox(scroll_page(vbox(columns, None, margins=(28, 22, 28, 24))), spacing=0))

        bot.bus.stream_status.connect(lambda _info: self.refresh())
        bot.bus.connection_state.connect(lambda *_: self.refresh())
        self._update_count()
        self.refresh()

    # ------------------------------------------------------------- estado
    def refresh(self) -> None:
        for p, card in self._status.items():
            client = self._bot.platforms[p]
            card.show_info(client.state is ConnectionState.CONNECTED, client.stream_info)
        self._set_enabled()

    def _set_enabled(self) -> None:
        any_on = bool(self._bot.stream.connected())
        for b in (self._apply_title, self._apply_game):
            b.setEnabled(any_on and not self._busy)
            b.setToolTip("" if any_on else "Conecta Twitch o Kick primero")

    def showEvent(self, event) -> None:  # noqa: N802 (Qt)
        super().showEvent(event)
        if not self._title_edit.text() and not self._game_edit.text():
            self._fill_current()

    def _fill_current(self) -> None:
        """Rellena los campos con lo de la primera plataforma elegida que tenga datos."""
        chosen = [p for p in Platform if p in self._picker.value()] or list(Platform)
        infos = [self._bot.platforms[p].stream_info for p in chosen]
        title = next((i.title for i in infos if i.title), "")
        game = next((i.game for i in infos if i.game), "")
        if title:
            self._title_edit.setText(title[:MAX_TITLE_LEN])
        if game:
            self._game_edit.setText(game)

    def _update_count(self) -> None:
        self._title_count.setText(f"{len(self._title_edit.text())}/{MAX_TITLE_LEN}")

    # ------------------------------------------------------------ búsqueda
    def _start_search(self) -> None:
        query = self._game_edit.text().strip()
        targets = self._targets()
        if len(query) < 2 or not targets:
            return
        if self._search_task is not None and not self._search_task.done():
            self._search_task.cancel()
        self._search_task = asyncio.ensure_future(self._search(targets[0], query))

    async def _search(self, platform: Platform, query: str) -> None:
        self._search_hint.setText(f"Buscando en {platform.label}…")
        try:
            found = await self._bot.stream.search(platform, query)
        except PlatformError as exc:
            self._search_hint.setText(str(exc))
            return
        if self._game_edit.text().strip() != query:
            return  # ya escribió otra cosa
        names = list(dict.fromkeys(c.name for c in found))
        self._game_model.setStringList(names)
        self._search_hint.setText(f"{len(names)} resultados en {platform.label}" if names
                                  else f"Sin resultados en {platform.label}")
        if names and self._game_edit.hasFocus():
            self._game_edit.completer().complete()

    # ------------------------------------------------------------- aplicar
    def _targets(self) -> list[Platform]:
        connected = set(self._bot.stream.connected())
        return [p for p in Platform if p in self._picker.value() and p in connected]

    def _show_result(self, text: str, ok: bool) -> None:
        self._result.setStyleSheet(f"color: {t.GREEN if ok else t.RED}; font-size: 12px;")
        self._result.setText(text)

    def _check(self, value: str, what: str) -> list[Platform] | None:
        if not value.strip():
            self._show_result(f"Escribe {what}.", False)
            return None
        targets = self._targets()
        if not targets:
            self._show_result("Elige al menos una plataforma conectada.", False)
            return None
        return targets

    def _on_apply_title(self) -> None:
        text = self._title_edit.text()
        targets = self._check(text, "un título")
        if targets:
            asyncio.ensure_future(self.apply_title(text, targets))

    def _on_apply_game(self) -> None:
        text = self._game_edit.text()
        targets = self._check(text, "una categoría")
        if targets:
            asyncio.ensure_future(self.apply_category(text, targets))

    async def apply_title(self, text: str, targets: list[Platform]) -> None:
        results = await self._busy_while(self._bot.stream.set_title(text, targets))
        done = [p.label for p, e in results.items() if not e]
        self._report(results, f"Título cambiado en {' y '.join(done)}." if done else "")

    async def apply_category(self, text: str, targets: list[Platform]) -> None:
        results, applied = await self._busy_while(self._bot.stream.set_category(text, targets))
        self._report(results, category_text(applied) if applied else "")
        if applied:
            self._game_edit.setText(next(iter(applied.values())).name)

    async def _busy_while(self, coro):
        self._busy = True
        self._set_enabled()
        self._show_result("Aplicando…", True)
        try:
            return await coro
        finally:
            self._busy = False
            self._set_enabled()

    def _report(self, results: dict, ok_text: str) -> None:
        failed = [f"{p.label}: {e}" for p, e in results.items() if e]
        self._show_result("\n".join(([ok_text] if ok_text else []) + failed), not failed)
