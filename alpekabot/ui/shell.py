"""Estructura de la ventana: barra lateral (logo, plataformas, menú, usuario) y barra «Sonando ahora»."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtWidgets import QButtonGroup, QFrame, QLabel, QProgressBar, QPushButton, QSlider, QWidget

from .. import __version__
from ..core.models import ConnectionState, Platform
from ..features.songs import SongStatus
from . import theme as t
from .widgets import (
    Avatar,
    Dot,
    Pill,
    fmt_mmss,
    frame,
    hbox,
    icon_button,
    icon_label,
    initials,
    label,
    soon,
    vbox,
)

HEADER_H = 64


# ================================================================ barra lateral
class NavItem(QPushButton):
    """Entrada del menú: icono + texto + contador/punto a la derecha."""

    def __init__(self, text: str, icon: str, icon_color: str | None = None) -> None:
        super().__init__()
        self.setObjectName("nav")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(42)
        self._icon_name, self._icon_color = icon, icon_color
        self._icon = QLabel()
        self._text = QLabel(text)
        self._badge = QLabel()
        self._badge.setFixedHeight(20)
        self._badge.setAlignment(Qt.AlignCenter)
        self._badge.hide()
        self._dot = Dot(t.GREEN, 7)
        self._dot.hide()
        for w in (self._icon, self._text, self._badge, self._dot):
            w.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setLayout(hbox(self._icon, 4, self._text, None, self._badge, self._dot,
                            spacing=8, margins=(12, 0, 12, 0)))
        self.toggled.connect(self._restyle)
        self._restyle()

    def _restyle(self) -> None:
        on, enabled = self.isChecked(), self.isEnabled()
        color = "#ffffff" if on else (t.TEXT_2 if enabled else t.TEXT_4)
        icon_color = self._icon_color if (self._icon_color and enabled) else color
        self._icon.setPixmap(t.pixmap(self._icon_name, icon_color, 18))
        self._text.setStyleSheet(f"color: {color}; font-size: 14px; {'font-weight: 600;' if on else ''}")

    def changeEvent(self, event) -> None:  # noqa: N802 (Qt)
        super().changeEvent(event)
        if event.type() == QEvent.EnabledChange:
            self._restyle()

    def set_badge(self, text: str, color: str = t.TEXT_3, filled: bool = False) -> None:
        self._badge.setVisible(bool(text))
        self._badge.setText(text)
        bg = t.alpha(color, 0.15) if filled else t.BG_800
        border = t.alpha(color, 0.5) if filled else t.BORDER
        self._badge.setStyleSheet(
            f"background: {bg}; border: 1px solid {border}; border-radius: 9px; color: {color};"
            f" font-family: '{t.MONO}'; font-size: 11px; font-weight: 600; padding: 1px 7px;"
        )

    def set_dot(self, color: str | None) -> None:
        self._dot.setVisible(color is not None)
        if color:
            self._dot.set_color(color)


class PlatformCard(QFrame):
    """«● Twitch #loon_vt   ONLINE»."""

    _STATES = {  # estado -> (texto, color o None = color de la plataforma)
        ConnectionState.DISCONNECTED: ("OFFLINE", t.TEXT_3),
        ConnectionState.CONNECTING: ("CONECTANDO", t.AMBER),
        ConnectionState.CONNECTED: ("ONLINE", None),
        ConnectionState.ERROR: ("ERROR", t.RED),
    }

    def __init__(self, platform: Platform) -> None:
        super().__init__()
        self.platform = platform
        self._color = t.PLATFORM_COLORS[platform.value]
        self._dot = Dot(self._color, 8)
        self._name = QLabel()
        self._name.setStyleSheet(f"font-family: '{t.MONO}'; font-size: 12px;")
        self._pill = Pill("OFFLINE", t.TEXT_3, size=10)
        self.setLayout(hbox(self._dot, self._name, None, self._pill, spacing=8, margins=(12, 9, 10, 9)))
        self._state = ConnectionState.DISCONNECTED
        self._live = False
        self._channel = ""
        self._render()

    def set_channel(self, channel: str) -> None:
        self._channel = channel
        self._render()

    def set_state(self, state: ConnectionState, detail: str = "") -> None:
        self._state = state
        if state is not ConnectionState.CONNECTED:
            self._live = False
        self.setToolTip(detail)
        self._render()

    def set_live(self, live: bool) -> None:
        self._live = live
        self._render()

    def _render(self) -> None:
        text, color = self._STATES[self._state]
        color = color or self._color
        active = self._state is ConnectionState.CONNECTED
        if self._live and active:
            text = "LIVE"
        self._pill.set(text, color)
        self._dot.set_color(self._color if active else t.TEXT_4)
        channel = f"#{self._channel}" if self._channel and self.platform is Platform.TWITCH else self._channel
        name_color = self._color if (self.platform is Platform.KICK and active) else t.TEXT
        self._name.setText(f"<span style='color:{name_color}'>{self.platform.label}</span> "
                           f"<span style='color:{t.TEXT_2}'>{channel}</span>")
        border = t.alpha(self._color, 0.45) if active else t.BORDER
        bg = t.alpha(self._color, 0.06) if active else t.BG_850
        self.setStyleSheet(f"PlatformCard {{ background: {bg}; border: 1px solid {border}; border-radius: 10px; }}")


class Sidebar(QFrame):
    page_requested = Signal(str)       # clave de la página
    settings_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("sidebar")
        self.setFixedWidth(290)

        # logo (zona de arrastre de la ventana)
        logo_box = QLabel()
        logo_box.setFixedSize(34, 34)
        logo_box.setAlignment(Qt.AlignCenter)
        logo_box.setPixmap(t.pixmap("bot", "#ffffff", 20))
        logo_box.setStyleSheet(f"background: {t.ACCENT}; border-radius: 9px;")
        title = label("ALPEKABOT", size=15, bold=True, mono=True)
        version = Pill(f"v{__version__}", t.TEXT_3, bg=0.08, border=0.3, size=10, bold=False)
        self.logo = QWidget()
        self.logo.setFixedHeight(HEADER_H)
        self.logo.setLayout(hbox(logo_box, 4, title, version, None, spacing=8, margins=(18, 0, 12, 0)))

        self.cards = {p: PlatformCard(p) for p in Platform}

        self.items: dict[str, NavItem] = {}
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        nav = vbox(spacing=4, margins=(12, 0, 12, 0))
        sections = [
            ("SECCIÓN PRINCIPAL", [("chat", "Chat en Vivo", "message-square", None),
                                   ("commands", "Comandos", "terminal", None),
                                   ("timers", "Timers", "clock", None),
                                   ("songs", "Canciones & SR", "music", t.AMBER),
                                   ("tts", "Texto a voz", "audio-lines", None)]),
            ("AUTOMATIZACIÓN", [("moderation", "Moderación & Filtros", "shield", None),
                                ("vtube", "VTube Studio", "smile", None)]),
            ("SISTEMA", [("settings", "Configuración", "settings", None),
                         ("console", "Consola & Logs", "terminal", None)]),
        ]
        for title_text, entries in sections:
            nav.addWidget(label(title_text, "sideSection"))
            for key, text, icon, color in entries:
                item = NavItem(text, icon, color)
                self.items[key] = item
                if key == "settings":
                    item.setCheckable(False)
                    item.clicked.connect(self.settings_requested.emit)
                else:
                    self._group.addButton(item)
                    item.clicked.connect(lambda _=False, k=key: self.page_requested.emit(k))
                nav.addWidget(item)

        # usuario (decorativo)
        self._avatar = Avatar("?", 40, t.ACCENT_SOFT, t.alpha(t.ACCENT, 0.15), t.alpha(t.ACCENT, 0.5))
        self._user = label("Sin cuenta", size=14, bold=True)
        self._presence_dot = Dot(t.TEXT_3, 7)
        self._presence_text = label("", size=11, mono=True)
        self._presence = QWidget()
        self._presence.setLayout(hbox(self._presence_dot, self._presence_text, None, spacing=6))
        power = icon_button("power", "", None, t.TEXT_3, 17, "iconBox")
        power.setFocusPolicy(Qt.NoFocus)
        user_box = frame("userCard", hbox(self._avatar, vbox(self._user, self._presence, spacing=2), None, power,
                                          spacing=12, margins=(16, 12, 14, 14)))
        user_box.setStyleSheet(f"QFrame#userCard {{ border-top: 1px solid {t.BORDER}; }}")

        cards = vbox(*self.cards.values(), spacing=8, margins=(14, 4, 14, 8))
        self.setLayout(vbox(self.logo, frame("hline"), 10, cards, frame("hline"), 4, nav, None, user_box,
                            spacing=0))
        self.set_presence(False)

    def select(self, key: str) -> None:
        if key in self.items:
            self.items[key].setChecked(True)

    def set_user(self, name: str) -> None:
        self._user.setText(name or "Sin cuenta")
        self._avatar.setText(initials(name or "?"))

    def set_presence(self, online: bool) -> None:
        color = t.GREEN if online else t.TEXT_3
        self._presence_dot.set_color(color)
        self._presence_text.setText(f"<span style='color:{color}'>{'En línea' if online else 'Desconectado'}</span>")


# ============================================================ barra inferior
class NowPlayingBar(QFrame):
    play_pause = Signal()
    skip = Signal()
    stop = Signal()
    volume_changed = Signal(int)
    console_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("nowBar")
        self.setStyleSheet(f"QFrame#nowBar {{ background: {t.BG_950}; border-top: 1px solid {t.BORDER}; }}")
        disc = QLabel()
        disc.setFixedSize(44, 44)
        disc.setAlignment(Qt.AlignCenter)
        disc.setPixmap(t.pixmap("disc", t.ACCENT_SOFT, 22))
        disc.setStyleSheet(f"background: {t.BG_850}; border: 1px solid {t.BORDER}; border-radius: 10px;")
        self._pill = Pill("EN ESPERA", t.TEXT_3, size=10)
        self._by = label("", color=t.TEXT_3, size=11, mono=True)
        self._title = label("Nada sonando", size=14, bold=True)
        info = QWidget()
        info.setFixedWidth(430)
        info.setLayout(vbox(hbox(self._pill, self._by, None, spacing=8), self._title, spacing=4))

        prev = soon(icon_button("skip-back", "", None, t.TEXT_2, 18), "canción anterior")
        self._play = icon_button("play", "Reproducir / pausar", self.play_pause.emit, "#ffffff", 16, "playBtn")
        self._play.setFixedSize(QSize(36, 36))
        nxt = icon_button("skip-forward", "Saltar", self.skip.emit, t.TEXT_2, 18)
        stop = icon_button("x", "Detener", self.stop.emit, t.TEXT_2, 18)
        self._cur = label("00:00", color=t.TEXT_3, size=11, mono=True)
        self._bar = QProgressBar()
        self._bar.setTextVisible(False)
        self._bar.setRange(0, 1000)
        self._total = label("--:--", color=t.TEXT_3, size=11, mono=True)
        center = QWidget()
        center.setLayout(vbox(hbox(None, prev, self._play, nxt, stop, None, spacing=14),
                              hbox(self._cur, self._bar, self._total, spacing=10), spacing=4))

        self._volume = QSlider(Qt.Horizontal)
        self._volume.setRange(0, 100)
        self._volume.setFixedWidth(110)
        self._volume.sliderReleased.connect(lambda: self.volume_changed.emit(self._volume.value()))
        sep = QFrame()
        sep.setFixedSize(1, 28)
        sep.setStyleSheet(f"background: {t.BORDER};")
        console = icon_button("terminal", "Consola & Logs", self.console_requested.emit, t.ACCENT_SOFT, 17, "iconBox")
        lay = hbox(disc, info, center, icon_label("volume-2", t.TEXT_2, 18), self._volume, 8, sep, 8, console,
                   spacing=14, margins=(20, 10, 18, 10))
        lay.setStretchFactor(center, 1)
        self.setLayout(lay)

    def set_status(self, st: SongStatus) -> None:
        s = st.current
        if s is None:
            self._pill.set("EN ESPERA", t.TEXT_3)
            self._by.setText("")
            self._title.setText("Nada sonando")
            self._title.setToolTip("")
            self.set_progress(0, 0)
        else:
            self._pill.set("EN PAUSA" if st.paused else "SONANDO AHORA", t.AMBER if st.paused else t.ACCENT_SOFT)
            self._by.setText("Respaldo" if st.from_backup or not s.requested_by
                             else f"Pedida por: <span style='color:{t.TEXT_2}'>{s.requested_by}</span>")
            self._title.setText(self._title.fontMetrics().elidedText(s.title, Qt.ElideRight, 420))
            self._title.setToolTip(s.title)
        self._play.setIcon(t.icon("pause" if s is not None and not st.paused else "play", "#ffffff", 16))
        if not self._volume.isSliderDown():
            self._volume.setValue(st.volume)

    def set_progress(self, pos: float, dur: float) -> None:
        self._cur.setText(fmt_mmss(pos))
        self._total.setText(fmt_mmss(dur) if dur else "--:--")
        self._bar.setValue(int(1000 * pos / dur) if dur else 0)
