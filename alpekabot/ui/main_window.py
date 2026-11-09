"""Ventana principal de AlpekaBOT (diseño «stitch_minimalist_desktop_ui_design»).

Ventana sin marco: barra lateral a la izquierda; a la derecha, la cabecera (migas
de pan, acciones de la página, Conectar, Ajustes y botones de la ventana), las
páginas y la barra «Sonando ahora».
"""

from __future__ import annotations

import asyncio
import logging

from PySide6.QtCore import QEvent, QPoint, Qt, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QFrame, QInputDialog, QLabel, QMainWindow, QSizePolicy, QStackedWidget, QWidget

from .. import APP_NAME, __version__
from ..bot import AlpekaBot
from ..core.models import AccountRole, ChatMessage, ConnectionState, Platform, StreamInfo
from ..features.moderation import Chatter, ModAction, format_duration
from ..features.songs import SongQueued, SongStatus
from ..features.updater import UpdateError, UpdateInfo
from ..features.youtube import format_duration as song_duration
from ..integrations.vtube_studio import VTSState, VTSStatus
from ..paths import IS_FROZEN
from ..platforms.base import PlatformError
from . import theme as t
from .chat_view import ChatView
from .commands_tab import CommandsTab
from .frameless import CORNER_RADIUS, FramelessHelper, WindowButtons, WindowOutline, corner_styles, is_caption_area
from .log_panel import LogPanel, QtLogHandler
from .moderation_tab import ModerationTab
from .settings_dialog import SettingsDialog
from .shell import HEADER_H, NowPlayingBar, Sidebar
from .songs_tab import SongsTab
from .timers_tab import TimersTab
from .tts_player import QtTtsPlayer
from .tts_tab import TtsTab
from .update_dialog import UpdateDialog
from .vtube_tab import VTubeTab
from .widgets import SearchBox, button, frame, hbox, icon_button, label, vbox
from .youtube_player import WebPlayer

log = logging.getLogger(__name__)

SHUTDOWN_TIMEOUT_S = 10
PROGRESS_POLL_MS = 1000
UPDATE_CHECK_DELAY_S = 5  # deja terminar el arranque antes de consultar GitHub


class MainWindow(QMainWindow):
    def __init__(self, bot: AlpekaBot, log_handler: QtLogHandler) -> None:
        super().__init__()
        self.bot = bot
        self._busy = False
        self._closing = False
        self._ready_to_close = False
        self._settings: SettingsDialog | None = None
        self._update_dialog: UpdateDialog | None = None
        self._pending_update: tuple[UpdateInfo, object] | None = None

        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1440, 900)
        self.setMinimumSize(1100, 680)
        self._frameless = FramelessHelper(self, self._is_caption)

        # ------------------------------------------------------------ páginas
        self.chat = ChatView()
        self.chat.set_bot_check(self._is_bot_message)
        self.chat.send_requested.connect(self._on_send)
        self.chat.moderation_requested.connect(self._on_moderation_requested)
        self.player = WebPlayer(self)
        bot.songs.attach_player(self.player)
        self.commands_page = CommandsTab(
            bot.commands, bot.bus, lambda: bot.builtin_commands,
            builtin_info=lambda: {bot.tts.config.command: ("mensaje", "Lee el mensaje en voz alta (texto a voz).", "TTS")},
        )
        self.timers_page = TimersTab(bot.timers, bot.bus)
        self.songs_page = SongsTab(bot.songs, bot.bus, self.player, self._owner_name, self._default_platform)
        self.tts_player = QtTtsPlayer(self)
        bot.tts.attach_player(self.tts_player)
        self.tts_page = TtsTab(bot.tts, bot.bus)
        self.moderation_page = ModerationTab(bot.moderation, bot.bus)
        self.vtube_page = VTubeTab(bot)
        self.console_page = LogPanel(log_handler)

        # acciones de la cabecera del chat
        self._chat_filter = SearchBox("Filtrar mensajes o usuarios…")
        self._chat_filter.setFixedWidth(300)
        self._chat_filter.textChanged.connect(self.chat.set_filter)
        clear_chat = icon_button("trash-2", "Limpiar el chat de la app", self.chat.clear, t.TEXT_2, 16, "iconBox")
        chat_actions = QWidget()
        chat_actions.setLayout(hbox(self._chat_filter, clear_chat, spacing=10))

        self._pages: dict[str, tuple[QWidget, str, str, QWidget | None]] = {
            "chat": (self.chat, "Chat en Vivo (Twitch + Kick)", "message-square", chat_actions),
            "commands": (self.commands_page, CommandsTab.title, CommandsTab.icon, self.commands_page.actions),
            "timers": (self.timers_page, TimersTab.title, TimersTab.icon, self.timers_page.actions),
            "songs": (self.songs_page, SongsTab.title, SongsTab.icon, self.songs_page.actions),
            "tts": (self.tts_page, TtsTab.title, TtsTab.icon, self.tts_page.actions),
            "moderation": (self.moderation_page, ModerationTab.title, ModerationTab.icon, None),
            "vtube": (self.vtube_page, VTubeTab.title, VTubeTab.icon, self.vtube_page.actions),
            "console": (self.console_page, LogPanel.title, LogPanel.icon, self.console_page.actions),
        }
        self._stack = QStackedWidget()
        self._stack.setObjectName("pages")
        for page, *_ in self._pages.values():
            self._stack.addWidget(page)

        # ----------------------------------------------------------- cabecera
        self._crumb_icon = QLabel()
        self._crumb_page = label("", "crumbPage", size=14)
        crumb = hbox(label("AlpekaBot", "crumbRoot", size=14), label("/", color=t.TEXT_4, size=14),
                     self._crumb_icon, self._crumb_page, spacing=10)
        self._actions_slot = hbox(spacing=0)
        self._crumb_page.setMinimumWidth(80)  # puede encogerse si las acciones de la página no caben
        self._crumb_page.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self._connect_btn = button("Conectar", "plug", "primary", self._toggle_connection)
        settings_btn = icon_button("sliders-horizontal", "Ajustes (cuentas, ngrok, YouTube)", self.open_settings,
                                   t.TEXT_2, 17, "iconBox")
        self._win_buttons = WindowButtons(self)
        sep = QFrame()
        sep.setFixedSize(1, 26)
        sep.setStyleSheet(f"background: {t.BORDER};")
        self._header = frame("header", hbox(crumb, None, self._actions_slot, 4, self._connect_btn, settings_btn, 8,
                                            sep, 6, self._win_buttons, spacing=10, margins=(24, 0, 10, 0)))
        self._header.setFixedHeight(HEADER_H)

        # ------------------------------------------------- lateral e inferior
        self.sidebar = Sidebar()
        self.sidebar.page_requested.connect(self.show_page)
        self.sidebar.settings_requested.connect(self.open_settings)
        self.now_bar = NowPlayingBar()
        self.now_bar.play_pause.connect(self._toggle_play)
        self.now_bar.skip.connect(lambda: bot.songs.skip())
        self.now_bar.stop.connect(lambda: bot.songs.stop())
        self.now_bar.volume_changed.connect(bot.songs.set_volume)
        self.now_bar.console_requested.connect(lambda: self.show_page("console"))

        right = QWidget()
        right.setObjectName("contentArea")  # transparente: las esquinas las pintan cabecera y barra inferior
        right.setLayout(vbox(self._header, self._stack, self.now_bar, spacing=0))
        root = QWidget()
        root.setObjectName("roundedRoot")
        root.setLayout(hbox(self.sidebar, right, spacing=0))
        self.setCentralWidget(root)
        # Esquinas redondeadas propias: ventana transparente y cada pieza del borde con su radio
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet("QMainWindow { background: transparent; } QWidget#roundedRoot, QWidget#contentArea"
                           " { background: transparent; }")
        self._outline = WindowOutline(root)
        self._apply_corners()

        # ------------------------------------------------------------ eventos
        bus = bot.bus
        bus.chat_message.connect(self._on_chat_message)
        bus.connection_state.connect(self._on_connection_state)
        bus.stream_status.connect(self._on_stream_status)
        bus.mod_action.connect(self._on_mod_action)
        bus.song_queued.connect(self._on_song_queued)
        bus.song_status.connect(self._on_song_status)
        bus.songs_changed.connect(self._update_counters)
        bus.commands_changed.connect(self._update_counters)
        bus.timers_changed.connect(self._update_counters)
        bus.tts_changed.connect(self._update_counters)
        bus.account_changed.connect(lambda *_: self._refresh_accounts())
        bus.vts_status.connect(self._on_vts_status)
        self.chat.count_changed.connect(lambda n: self.sidebar.items["chat"].set_badge(str(n), t.TEXT_2))

        self._progress = QTimer(self)
        self._progress.setInterval(PROGRESS_POLL_MS)
        self._progress.timeout.connect(self._poll_progress)
        self._progress.start()

        self._refresh_accounts()
        self._update_counters()
        self._on_song_status(bot.songs.status)
        self.show_page("chat")
        self._update_actions()

    # ------------------------------------------------------- ventana sin marco
    def _is_caption(self, pos: QPoint) -> bool:
        return is_caption_area(self._header, self, pos) or is_caption_area(self.sidebar.logo, self, pos)

    def nativeEvent(self, event_type, message):  # noqa: N802 (Qt)
        result = self._frameless.native_event(event_type, message)
        return result if result is not None else super().nativeEvent(event_type, message)

    def showEvent(self, event) -> None:  # noqa: N802 (Qt)
        super().showEvent(event)
        self._frameless.after_show()

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt)
        if event.button() == Qt.LeftButton and self._frameless.mouse_press(event.position().toPoint()):
            return
        super().mousePressEvent(event)

    def changeEvent(self, event) -> None:  # noqa: N802 (Qt)
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange:
            self._win_buttons.update_state()
            self._apply_corners()

    def _apply_corners(self) -> None:
        """Maximizada, la ventana toca los bordes de la pantalla: sin esquinas redondeadas."""
        radius = 0 if (self.isMaximized() or self.isFullScreen()) else CORNER_RADIUS
        styles = corner_styles(radius)
        self.sidebar.setStyleSheet(styles["sidebar"])
        self._header.setStyleSheet(styles["header"])
        self.now_bar.setStyleSheet(styles["nowBar"])
        self._outline.radius = radius
        self._outline.update()

    # ------------------------------------------------------------- navegación
    def show_page(self, key: str) -> None:
        page, title, icon, actions = self._pages[key]
        self._stack.setCurrentWidget(page)
        self._crumb_icon.setPixmap(t.pixmap(icon, t.ACCENT_SOFT, 17))
        self._crumb_page.setText(title)
        while self._actions_slot.count():
            item = self._actions_slot.takeAt(0)
            if item.widget() is not None:
                item.widget().hide()
                item.widget().setParent(None)
        if actions is not None:
            self._actions_slot.addWidget(actions)
            actions.show()
        self.sidebar.select(key)

    # --------------------------------------------------------------- arranque
    def on_startup(self) -> None:
        self.bot.start()
        asyncio.ensure_future(self._start_player())
        if IS_FROZEN:  # desde el código fuente se actualiza con git
            asyncio.ensure_future(self._auto_check_updates())
        cfg = self.bot.config.twitch
        if not any(c.is_configured for c in self.bot.platforms.values()):
            self.chat.add_system("Bienvenido a AlpekaBOT. Configura Twitch o Kick en «Ajustes» para empezar.")
            self.open_settings()
        elif cfg.auto_connect:
            self._toggle_connection()
        else:
            self.chat.add_system("Pulsa «Conectar» (arriba a la derecha) para entrar al chat.")

    async def _start_player(self) -> None:
        try:
            await self.player.start()
        except Exception:
            log.exception("No se pudo iniciar el reproductor de YouTube")

    # --------------------------------------------------------- actualizaciones
    async def _auto_check_updates(self) -> None:
        """Consulta silenciosa al abrir: un fallo aquí no debe molestar al usuario."""
        await asyncio.sleep(UPDATE_CHECK_DELAY_S)
        updater = self.bot.updater
        try:
            updater.cleanup()
            info = await updater.check()
        except UpdateError as exc:
            log.info("Actualizaciones: %s", exc)
            return
        except Exception:
            log.exception("Error comprobando actualizaciones")
            return
        if info is not None:
            self.show_update(info)

    def show_update(self, info: UpdateInfo) -> None:
        if self._closing or self._update_dialog is not None:
            return
        if self._settings is not None:  # no apilar ventanas modales
            self.chat.add_system(f"AlpekaBOT {info.version} disponible: Ajustes → Actualizaciones.")
            return
        dialog = UpdateDialog(self.bot.updater, info, self.bot.any_connected, self)
        dialog.install_requested.connect(self._install_update)
        dialog.finished.connect(lambda _: setattr(self, "_update_dialog", None))
        self._update_dialog = dialog
        dialog.open()

    def _install_update(self, info: UpdateInfo, path) -> None:
        """El instalador se lanza al final del cierre ordenado (ver ``_shutdown``)."""
        self._pending_update = (info, path)
        self.close()

    # ------------------------------------------------------------- acciones
    def open_settings(self) -> None:
        if self._settings is None:
            self._settings = SettingsDialog(self.bot, self)
            self._settings.finished.connect(lambda _: setattr(self, "_settings", None))
            self._settings.update_found.connect(self.show_update)
            self._settings.open()
        self._settings.raise_()

    def _toggle_connection(self) -> None:
        if self._busy:
            return
        asyncio.ensure_future(self._disconnect() if self.bot.any_connected else self._connect())

    async def _connect(self) -> None:
        self._busy = True
        self._update_actions()
        try:
            for err in await self.bot.connect_all():
                self.chat.add_system(err, "error")
        finally:
            self._busy = False
            self._update_actions()

    async def _disconnect(self) -> None:
        self._busy = True
        self._update_actions()
        try:
            await self.bot.disconnect_all()
        finally:
            self._busy = False
            self._update_actions()

    def _on_send(self, platform: Platform | None, text: str) -> None:
        asyncio.ensure_future(self._send(platform, text))

    async def _send(self, platform: Platform | None, text: str) -> None:
        targets = [platform] if platform else [p for p, c in self.bot.platforms.items() if c.is_connected]
        ok = False
        for p in targets:
            try:
                await self.bot.send_message(p, text)
                ok = True
            except PlatformError as exc:
                self.chat.add_system(f"{p.label}: {exc}", "error")
            except Exception:
                log.exception("Error enviando mensaje a %s", p.label)
                self.chat.add_system(f"{p.label}: error enviando el mensaje", "error")
        if ok:
            self.chat.clear_input()

    def _toggle_play(self) -> None:
        st = self.bot.songs.status
        if st.current is not None and not st.paused:
            self.bot.songs.pause()
        else:
            self.bot.songs.play()

    # ------------------------------------------------------------ moderación
    def _on_moderation_requested(self, action: str, msg: ChatMessage) -> None:
        """Clic derecho sobre un mensaje del chat."""
        seconds, reason = 0, ""
        if action == "timeout:custom":
            # Diálogos estáticos breves: se toleran (ver CLAUDE.md, «Ventanas en la UI»)
            minutes, ok = QInputDialog.getInt(
                self, "Timeout", f"Minutos de timeout para {msg.user_name}:", 10, 1, 20_160
            )
            if not ok:
                return
            action, seconds = "timeout", minutes * 60
        elif action.startswith("timeout:"):
            action, seconds = "timeout", int(action.split(":", 1)[1])
        if action in ("timeout", "ban"):
            prompt = f"Motivo (opcional) para {'banear' if action == 'ban' else 'el timeout de'} {msg.user_name}:"
            reason, ok = QInputDialog.getText(self, "Banear" if action == "ban" else "Timeout", prompt)
            if not ok:
                return
        asyncio.ensure_future(self._moderate(action, msg, seconds, reason.strip()))

    async def _moderate(self, action: str, msg: ChatMessage, seconds: int, reason: str) -> None:
        mod = self.bot.moderation
        target = Chatter(msg.user_id, msg.user_name, msg.badges)
        try:
            if action == "delete":
                await mod.delete(msg)
            elif action == "timeout":
                await mod.timeout(msg.platform, target, seconds, reason)
            elif action == "ban":
                await mod.ban(msg.platform, target, reason)
            elif action == "unban":
                await mod.unban(msg.platform, target)
            elif action == "permit":
                secs = mod.permit(msg.platform, msg.user_login, msg.user_name)
                await self.bot.send_message(
                    msg.platform, f"@{msg.user_name} puedes enviar un enlace durante {format_duration(secs)}."
                )
        except PlatformError as exc:
            self.chat.add_system(f"{msg.platform.label}: {exc}", "error")
        except Exception:
            log.exception("Error de moderación")
            self.chat.add_system("Error inesperado al moderar (mira la consola)", "error")

    def _on_mod_action(self, a: ModAction) -> None:
        by = f"{a.moderator}, auto" if a.source == "auto" else a.moderator
        reason = f" — {a.reason}" if a.reason else ""
        self.chat.add_mod_action(a.platform, f"{a.user_name}: {a.label}{reason} ({by})")

    # ---------------------------------------------------------------- eventos
    def _is_bot_message(self, msg: ChatMessage) -> bool:
        client = self.bot.platforms.get(msg.platform)
        return client is not None and client.bot_user_id is not None and msg.user_id == client.bot_user_id

    def _on_chat_message(self, msg: ChatMessage) -> None:
        self.chat.add_message(msg)

    def _on_song_queued(self, ev: SongQueued) -> None:
        client = self.bot.platforms.get(ev.platform)
        bot_name = getattr(getattr(client, "bot", None), "display_name", "") or "AlpekaBot"
        self.chat.add_song_card(ev.platform, bot_name, ev.song.title, song_duration(ev.song.duration_s),
                                ev.position, ev.playing, ev.reply)

    def _on_song_status(self, st: SongStatus) -> None:
        self.now_bar.set_status(st)
        if st.current is None:
            self.songs_page.set_progress(0, 0)

    def _poll_progress(self) -> None:
        # También en pausa: se puede adelantar sin reanudar y la barra inferior debe enterarse
        if self.bot.songs.status.current is not None:
            self.player.query_progress(self._on_progress)

    def _on_progress(self, pos: float, dur: float) -> None:
        if self.bot.songs.status.current is None:
            return
        self.now_bar.set_progress(pos, dur)
        self.songs_page.set_progress(pos, dur)

    def _on_connection_state(self, platform: Platform, state: ConnectionState, detail: str) -> None:
        self.sidebar.cards[platform].set_state(state, detail)
        if state is ConnectionState.CONNECTED:
            self.chat.add_system(f"{platform.label}: conectado ({detail})", "ok")
            self._refresh_accounts()
        elif state is ConnectionState.DISCONNECTED:
            self.chat.add_system(f"{platform.label}: desconectado")
        elif state is ConnectionState.ERROR:
            self.chat.add_system(f"{platform.label}: error al conectar ({detail})", "error")
        self._update_actions()

    def _on_stream_status(self, info: StreamInfo) -> None:
        card = self.sidebar.cards.get(info.platform)
        if card is not None:
            card.set_live(info.is_live)
            card.setToolTip(f"{info.title}\n{info.game}" if info.is_live else "")

    def _on_vts_status(self, st: VTSStatus) -> None:
        color = {VTSState.CONNECTED: t.GREEN, VTSState.OFF: None}.get(st.state, t.AMBER)
        self.sidebar.items["vtube"].set_dot(color)

    # ---------------------------------------------------------------- estado
    def _owner_name(self) -> str:
        for p in Platform:
            acc = self._account(p, AccountRole.STREAMER)
            if acc:
                return acc.display_name
        return "Streamer"

    def _default_platform(self) -> Platform:
        connected = [p for p, c in self.bot.platforms.items() if c.is_connected]
        return connected[0] if connected else Platform.TWITCH

    def _account(self, platform: Platform, role: AccountRole):
        try:
            return self.bot.account(platform, role)
        except Exception:  # keyring no disponible
            return None

    def _refresh_accounts(self) -> None:
        for p, card in self.sidebar.cards.items():
            acc = self._account(p, AccountRole.STREAMER)
            card.set_channel(acc.login if acc else "sin cuenta")
        self.sidebar.set_user(self._owner_name())

    def _update_counters(self) -> None:
        items = self.sidebar.items
        items["commands"].set_badge(str(self.commands_page.count), t.TEXT_3)
        items["timers"].set_dot(t.GREEN if self.timers_page.any_enabled else None)
        pending = len(self.bot.songs.repo.queue_pending())
        items["songs"].set_badge(str(pending) if pending else "", t.AMBER, True)
        tts_pending = len(self.bot.tts.pending)
        items["tts"].set_badge(str(tts_pending) if tts_pending else "", t.GREEN, True)

    def _update_actions(self) -> None:
        connected = self.bot.any_connected
        b = self._connect_btn
        if self._busy:
            b.setText("Conectando…" if not connected else "Desconectando…")
            b.setEnabled(False)
        else:
            b.setEnabled(True)
            b.setText("Desconectar" if connected else "Conectar")
            b.setObjectName("" if connected else "primary")
            b.setIcon(t.icon("unplug" if connected else "plug", t.TEXT_2 if connected else "#ffffff", 16))
            b.style().unpolish(b)
            b.style().polish(b)
        self.sidebar.set_presence(connected)
        self.chat.set_platforms([p for p, c in self.bot.platforms.items() if c.is_connected])

    # ------------------------------------------------------------------ cierre
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt)
        if self._ready_to_close:
            event.accept()
            return
        event.ignore()
        if not self._closing:
            self._closing = True
            self.setEnabled(False)
            self._progress.stop()
            asyncio.ensure_future(self._shutdown())

    async def _shutdown(self) -> None:
        try:
            await asyncio.wait_for(self.bot.shutdown(), SHUTDOWN_TIMEOUT_S)
            await self.player.close()
        except Exception:
            log.exception("Error al cerrar")
        if self._pending_update is not None:
            try:
                await self.bot.updater.launch(*self._pending_update)
            except UpdateError as exc:  # al volver a abrir la app se ofrecerá de nuevo
                log.error("Actualización: %s", exc)
        self._ready_to_close = True
        self.close()


