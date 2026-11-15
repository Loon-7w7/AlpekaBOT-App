"""Editor del overlay «Sonando ahora» para OBS: activar/desactivar, URL, diseño y vista previa en vivo.

Cada cambio se aplica al momento (en OBS y en la vista previa) y se guarda en config.json.
"""

from __future__ import annotations

import asyncio
from dataclasses import fields
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QFont, QGuiApplication
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QColorDialog,
    QComboBox,
    QFontComboBox,
    QFrame,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QWidget,
)

from ..config import OverlayConfig
from ..features.overlay import ANIMATIONS, LIMITS
from . import theme as t
from .widgets import ModalDialog, Toggle, button, frame, hbox, icon_label, label, vbox

if TYPE_CHECKING:
    from ..bot import AlpekaBot

KEEP_ON_RESET = {"enabled", "port"}
SAVE_DELAY_MS = 200
CSS_DELAY_MS = 600

CONTENT = [
    ("show_header", "Encabezado («Sonando ahora»)"),
    ("show_cover", "Portada"),
    ("show_channel", "Canal de YouTube"),
    ("show_requester", "Quién la pidió"),
    ("show_platform", "Plataforma (Twitch / Kick)"),
    ("show_progress", "Barra de progreso"),
    ("show_time", "Tiempo (1:23 / 3:40)"),
]
COLORS = [
    ("accent", "Color de acento"),
    ("background", "Fondo"),
    ("text_color", "Texto"),
    ("subtext_color", "Texto secundario"),
]
SLIDERS = [
    ("font_scale", "Tamaño del texto", "%"),
    ("width", "Ancho de la tarjeta", " px"),
    ("bg_opacity", "Opacidad del fondo", "%"),
    ("radius", "Bordes redondeados", " px"),
    ("cover_size", "Tamaño de la portada", " px"),
    ("cover_radius", "Redondeo de la portada", " px"),
]
STYLE_TOGGLES = [
    ("border", "Borde con el color de acento"),
    ("shadow", "Sombra"),
    ("blur_cover", "Fondo difuminado con la portada"),
]
BEHAVIOR = [
    ("hide_when_idle", "Ocultar cuando no suena nada"),
    ("hide_when_paused", "Ocultar en pausa"),
    ("show_backup", "Mostrar las canciones del respaldo"),
]


class OverlayDialog(ModalDialog):
    def __init__(self, bot: "AlpekaBot", parent=None) -> None:
        super().__init__("Overlay para OBS", "Tarjeta «Sonando ahora» con portada. Se ve en OBS como una "
                         "Fuente de navegador y cada cambio se aplica al momento.", "monitor", parent,
                         width=1080)
        self.bot = bot
        self.cfg: OverlayConfig = bot.config.overlay
        self._loading = False
        self._toggles: dict[str, Toggle] = {}
        self._sliders: dict[str, tuple[QSlider, object]] = {}
        self._colors: dict[str, QPushButton] = {}
        self._save_timer = QTimer(self, singleShot=True, interval=SAVE_DELAY_MS, timeout=self.bot.save_overlay_style)
        self._css_timer = QTimer(self, singleShot=True, interval=CSS_DELAY_MS, timeout=self._apply_css)

        left = QWidget()
        left.setLayout(self._build_left())
        right = QScrollArea()
        right.setWidgetResizable(True)
        right.setFrameShape(QFrame.NoFrame)
        right.setFixedWidth(400)
        right.setMinimumHeight(520)
        inner = QWidget()
        inner.setLayout(self._build_controls())
        right.setWidget(inner)
        row = hbox(left, right, spacing=22)
        row.setStretchFactor(left, 1)
        self.body.addLayout(row)

        self.footer.addWidget(button("Restablecer diseño", "refresh-cw", slot=self._reset))
        self.footer.addStretch(1)
        self.footer.addWidget(button("Cerrar", object_name="primary", slot=self.accept))

        self._load_values()
        self._refresh_server()

    # ------------------------------------------------------------ columna izquierda
    def _build_left(self):
        self.enabled = Toggle(self.cfg.enabled)
        self.enabled.toggled.connect(lambda on: asyncio.ensure_future(self._restart(on, None)))
        self._status = label("", size=12, mono=True, wrap=True)

        self.preview = QWebEngineView()
        self.preview.page().setBackgroundColor(Qt.transparent)
        self.preview.setFixedHeight(210)
        self.preview.setContextMenuPolicy(Qt.NoContextMenu)
        off = label("El overlay está desactivado.", color=t.TEXT_3, size=13)
        off.setAlignment(Qt.AlignCenter)
        self._preview_stack = QStackedWidget()
        self._preview_stack.addWidget(self.preview)
        self._preview_stack.addWidget(off)
        stage = frame("overlayStage", vbox(self._preview_stack, margins=(8, 8, 8, 8)))
        stage.setStyleSheet("QFrame#overlayStage { border-radius: 12px; border: 1px solid %s;"
                            " background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #3a4157, stop:1 #1b1f2b); }"
                            % t.BORDER)

        self.url = QLineEdit()
        self.url.setReadOnly(True)
        self._copy = button("Copiar URL", "copy", "outlineAccent", self._copy_url, icon_color=t.ACCENT_SOFT)
        self.port = QSpinBox()
        self.port.setRange(*LIMITS["port"])
        self.port.setValue(self.cfg.port)
        self.port.setFixedWidth(100)
        apply_port = button("Cambiar puerto", slot=lambda: asyncio.ensure_future(
            self._restart(self.cfg.enabled, self.port.value())))

        return vbox(
            hbox(icon_label("power", t.ACCENT_SOFT, 16), label("<b>Overlay activado</b>"), None, self.enabled,
                 spacing=8),
            self._status,
            label("VISTA PREVIA", "sectionTitle"),
            stage,
            label("Si no suena nada, la vista previa enseña una canción de ejemplo.", color=t.TEXT_3, size=11),
            8,
            label("URL PARA OBS", "sectionTitle"),
            hbox(self.url, self._copy, spacing=8),
            label("En OBS: <b>+</b> → <b>Navegador</b> → pega la URL. Tamaño recomendado: el ancho de la tarjeta "
                  "+ 40 px y 200 px de alto. Solo funciona en este PC y con la app abierta.",
                  color=t.TEXT_2, size=12, wrap=True),
            hbox(label("Puerto", color=t.TEXT_2, size=12, mono=True), self.port, apply_port, None, spacing=8),
            label("Si cambias el puerto, la URL cambia: vuelve a pegarla en OBS.", color=t.TEXT_3, size=11,
                  wrap=True),
            None,
            spacing=10,
        )

    # ------------------------------------------------------------ controles
    def _section(self, icon: str, title: str):
        return hbox(icon_label(icon, t.ACCENT_SOFT, 15), label(title, "sectionTitle"), None, spacing=8)

    def _toggle_row(self, name: str, text: str):
        tg = Toggle(False)
        tg.toggled.connect(lambda on, n=name: self._set(n, on))
        self._toggles[name] = tg
        return hbox(label(text, color=t.TEXT_2, size=12), None, tg)

    def _build_controls(self):
        lay = vbox(spacing=10, margins=(0, 0, 10, 0))
        lay.addLayout(self._section("list-ordered", "CONTENIDO"))
        self.header_text = QLineEdit()
        self.header_text.setMaxLength(40)
        self.header_text.setPlaceholderText("Sonando ahora")
        self.header_text.textChanged.connect(lambda txt: self._set("header_text", txt))
        lay.addWidget(label("Texto del encabezado", color=t.TEXT_2, size=12))
        lay.addWidget(self.header_text)
        for name, text in CONTENT:
            lay.addLayout(self._toggle_row(name, text))

        lay.addSpacing(8)
        lay.addLayout(self._section("sliders-horizontal", "ESTILO"))
        self.font = QFontComboBox()
        self.font.currentFontChanged.connect(lambda f: self._set("font", f.family()))
        lay.addWidget(label("Fuente", color=t.TEXT_2, size=12))
        lay.addWidget(self.font)
        for name, text in COLORS:
            btn = QPushButton()
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFixedSize(64, 26)
            btn.clicked.connect(lambda _=False, n=name: self._pick_color(n))
            self._colors[name] = btn
            lay.addLayout(hbox(label(text, color=t.TEXT_2, size=12), None, btn))
        for name, text, unit in SLIDERS:
            sl = QSlider(Qt.Horizontal)
            sl.setRange(*LIMITS[name])
            value = label("", color=t.TEXT, size=12, mono=True, bold=True)
            sl.valueChanged.connect(lambda v, n=name, lbl=value, u=unit: (lbl.setText(f"{v}{u}"), self._set(n, v)))
            self._sliders[name] = (sl, value)
            lay.addLayout(hbox(label(text, color=t.TEXT_2, size=12), None, value))
            lay.addWidget(sl)
        for name, text in STYLE_TOGGLES:
            lay.addLayout(self._toggle_row(name, text))

        lay.addSpacing(8)
        lay.addLayout(self._section("settings", "COMPORTAMIENTO"))
        for name, text in BEHAVIOR:
            lay.addLayout(self._toggle_row(name, text))
        self.animation = QComboBox()
        for key, text in ANIMATIONS.items():
            self.animation.addItem(text, key)
        self.animation.currentIndexChanged.connect(lambda _i: self._set("animation", self.animation.currentData()))
        lay.addLayout(hbox(label("Animación al cambiar", color=t.TEXT_2, size=12), None, self.animation))

        lay.addSpacing(8)
        lay.addLayout(self._section("code-xml", "CSS PROPIO (AVANZADO)"))
        self.css = QPlainTextEdit()
        self.css.setPlaceholderText("#title { text-transform: uppercase; }\n#card { border: 2px solid gold; }")
        self.css.setFont(QFont("JetBrains Mono", 10))
        self.css.setFixedHeight(130)
        self.css.textChanged.connect(lambda: None if self._loading else self._css_timer.start())
        lay.addWidget(self.css)
        lay.addWidget(label("Elementos: #card, #cover, #header, #title, #channel, #who, #platform, #track, #fill, "
                            "#time.", color=t.TEXT_3, size=11, wrap=True))
        lay.addStretch(1)
        return lay

    # ------------------------------------------------------------ valores
    def _load_values(self) -> None:
        self._loading = True
        try:
            c = self.cfg
            self.header_text.setText(c.header_text)
            for name, tg in self._toggles.items():
                tg.setChecked(bool(getattr(c, name)))
            self.font.setCurrentFont(QFont(c.font))
            for name, (sl, _value) in self._sliders.items():
                sl.setValue(int(getattr(c, name)))
                sl.valueChanged.emit(sl.value())  # pone el texto aunque el valor no cambie
            for name in self._colors:
                self._paint_color(name)
            self.animation.setCurrentIndex(max(0, self.animation.findData(c.animation)))
            self.css.setPlainText(c.custom_css)
        finally:
            self._loading = False

    def _set(self, name: str, value) -> None:
        if self._loading or getattr(self.cfg, name) == value:
            return
        setattr(self.cfg, name, value)
        self._save_timer.start()

    def _apply_css(self) -> None:
        self._set("custom_css", self.css.toPlainText())

    def _paint_color(self, name: str) -> None:
        color = getattr(self.cfg, name)
        self._colors[name].setStyleSheet(f"background: {color}; border: 1px solid {t.BORDER}; border-radius: 6px;")
        self._colors[name].setToolTip(color)

    def _pick_color(self, name: str) -> None:
        dlg = QColorDialog(QColor(getattr(self.cfg, name)), self)
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        dlg.colorSelected.connect(lambda c, n=name: (self._set(n, c.name()), self._paint_color(n)))
        dlg.open()  # nunca exec(): frena qasync

    def _reset(self) -> None:
        defaults = OverlayConfig()
        for f in fields(OverlayConfig):
            if f.name not in KEEP_ON_RESET:
                setattr(self.cfg, f.name, getattr(defaults, f.name))
        self._load_values()
        self.bot.save_overlay_style()

    # ------------------------------------------------------------ servidor
    async def _restart(self, enabled: bool, port: int | None) -> None:
        self.enabled.setEnabled(False)
        try:
            await self.bot.set_overlay(enabled, port)
        finally:
            self.enabled.setEnabled(True)
            self._refresh_server()

    def _refresh_server(self) -> None:
        srv = self.bot.overlay
        self.url.setText(srv.url)
        self.url.setCursorPosition(0)
        if srv.running:
            self._status.setText(f"<span style='color:{t.GREEN}'>Funcionando en el puerto {srv.port}.</span>")
            self._preview_stack.setCurrentIndex(0)
            self.preview.setUrl(QUrl(srv.preview_url))
        else:
            text = srv.last_error or "Desactivado: OBS no mostrará la tarjeta."
            color = t.RED if srv.last_error else t.TEXT_3
            self._status.setText(f"<span style='color:{color}'>{text}</span>")
            self._preview_stack.setCurrentIndex(1)
            self.preview.setUrl(QUrl("about:blank"))
        self._copy.setEnabled(srv.running)

    def _copy_url(self) -> None:
        QGuiApplication.clipboard().setText(self.bot.overlay.url)
        self._copy.setText("¡Copiada!")
        QTimer.singleShot(1500, lambda: self._copy.setText("Copiar URL"))

    def done(self, result: int) -> None:
        if self._save_timer.isActive() or self._css_timer.isActive():
            self._css_timer.stop()
            self.cfg.custom_css = self.css.toPlainText()
            self._save_timer.stop()
            self.bot.save_overlay_style()
        self.preview.setUrl(QUrl("about:blank"))
        super().done(result)
