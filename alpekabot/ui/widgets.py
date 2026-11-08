"""Componentes visuales reutilizables del diseño: insignias, interruptores, tarjetas, modales…"""

from __future__ import annotations

from typing import Callable, Iterable

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QButtonGroup,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.models import Platform
from . import theme as t


# ------------------------------------------------------------------ layouts
def hbox(*items, spacing: int = 8, margins: tuple[int, int, int, int] = (0, 0, 0, 0)) -> QHBoxLayout:
    lay = QHBoxLayout()
    lay.setSpacing(spacing)
    lay.setContentsMargins(*margins)
    _add(lay, items)
    return lay


def vbox(*items, spacing: int = 8, margins: tuple[int, int, int, int] = (0, 0, 0, 0)) -> QVBoxLayout:
    lay = QVBoxLayout()
    lay.setSpacing(spacing)
    lay.setContentsMargins(*margins)
    _add(lay, items)
    return lay


def _add(lay, items) -> None:
    """Admite widgets, layouts, None (= estiramiento) y enteros (= espacio fijo)."""
    for it in items:
        if it is None:
            lay.addStretch(1)
        elif isinstance(it, int):
            lay.addSpacing(it)
        elif isinstance(it, QLayout):
            lay.addLayout(it)
        else:
            lay.addWidget(it)


class FlowLayout(QLayout):
    """Coloca los widgets en fila y salta de línea cuando no caben (chips de variables)."""

    def __init__(self, spacing: int = 8) -> None:
        super().__init__()
        self._items = []
        self.setSpacing(spacing)
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:  # noqa: N802 (Qt)
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, i: int):  # noqa: N802 (Qt)
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i: int):  # noqa: N802 (Qt)
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):  # noqa: N802 (Qt)
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 (Qt)
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 (Qt)
        return self._arrange(QRect(0, 0, width, 0), dry=True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802 (Qt)
        super().setGeometry(rect)
        self._arrange(rect, dry=False)

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt)
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802 (Qt)
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        return size

    def _arrange(self, rect: QRect, dry: bool) -> int:
        x, y, line_h, sp = rect.x(), rect.y(), 0, self.spacing()
        for it in self._items:
            hint = it.sizeHint()
            if x + hint.width() > rect.right() + 1 and line_h > 0:
                x, y, line_h = rect.x(), y + line_h + sp, 0
            if not dry:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + sp
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y()


def clear_layout(lay: QLayout) -> None:
    while lay.count():
        item = lay.takeAt(0)
        if item.widget() is not None:
            item.widget().deleteLater()
        elif item.layout() is not None:
            clear_layout(item.layout())


def frame(object_name: str = "card", layout: QLayout | None = None) -> QFrame:
    f = QFrame()
    f.setObjectName(object_name)
    if layout is not None:
        f.setLayout(layout)
    return f


def hline() -> QFrame:
    return frame("hline")


# ----------------------------------------------------------------- etiquetas
def label(text: str = "", object_name: str = "", *, size: int | None = None, color: str | None = None,
          bold: bool = False, mono: bool = False, wrap: bool = False) -> QLabel:
    lbl = QLabel(text)
    if object_name:
        lbl.setObjectName(object_name)
    css = []
    if size:
        css.append(f"font-size: {size}px")
    if color:
        css.append(f"color: {color}")
    if bold:
        css.append("font-weight: 600")
    if mono:
        css.append(f'font-family: "{t.MONO}"')
    if css:
        lbl.setStyleSheet("; ".join(css) + ";")
    lbl.setWordWrap(wrap)
    return lbl


def icon_label(name: str, color: str = t.TEXT_2, size: int = 16) -> QLabel:
    lbl = QLabel()
    lbl.setPixmap(t.pixmap(name, color, size))
    lbl.setFixedSize(size, size)
    return lbl


class Pill(QFrame):
    """Insignia pequeña: «ONLINE», «Broadcaster», «MOD», «YT»…"""

    def __init__(self, text: str = "", color: str = t.ACCENT_SOFT, *, bg: float = 0.12, border: float = 0.45,
                 icon: str | None = None, mono: bool = True, size: int = 11, bold: bool = True) -> None:
        super().__init__()
        self._icon = QLabel()
        self._text = QLabel(text)
        self.setLayout(hbox(self._icon, self._text, spacing=4, margins=(6, 1, 6, 1)))
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._size, self._mono, self._bold, self._bg, self._border = size, mono, bold, bg, border
        self.set(text, color, icon)

    def set(self, text: str, color: str, icon: str | None = None) -> None:
        self._text.setText(text)
        self._icon.setVisible(bool(icon))
        if icon:
            self._icon.setPixmap(t.pixmap(icon, color, self._size + 1))
        family = t.MONO if self._mono else t.SANS
        weight = 500 if self._bold else 400
        self.setStyleSheet(
            f"Pill {{ background: {t.alpha(color, self._bg)}; border: 1px solid {t.alpha(color, self._border)};"
            f" border-radius: 5px; }}"
            f" QLabel {{ color: {color}; font-family: '{family}'; font-size: {self._size}px; font-weight: {weight}; }}"
        )
        self._text.setVisible(bool(text))


class Dot(QLabel):
    def __init__(self, color: str = t.GREEN, size: int = 8) -> None:
        super().__init__()
        self.setFixedSize(size, size)
        self._size = size
        self.set_color(color)

    def set_color(self, color: str) -> None:
        self.setStyleSheet(f"background: {color}; border-radius: {self._size // 2}px;")


class StatLine(QWidget):
    """«● 22 Activos · ● 2 Pausados» con puntos dibujados (no dependen de la fuente)."""

    def __init__(self) -> None:
        super().__init__()
        self.setLayout(hbox(spacing=6))

    def set(self, items: list[tuple[str, str]]) -> None:
        lay = self.layout()
        clear_layout(lay)
        for i, (color, text) in enumerate(items):
            if i:
                lay.addWidget(label("·", color=t.TEXT_4, size=12, mono=True))
            lay.addWidget(Dot(color, 7))
            lay.addWidget(label(text, color=t.TEXT_2, size=12, mono=True))


class Avatar(QLabel):
    """Cuadrado redondeado con iniciales (o un icono)."""

    def __init__(self, text: str = "", size: int = 40, color: str = t.TEXT, bg: str = t.BG_800,
                 border: str = t.BORDER, icon: str | None = None, radius: int = 10) -> None:
        super().__init__()
        self.setFixedSize(size, size)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet(
            f"background: {bg}; border: 1px solid {border}; border-radius: {radius}px; color: {color};"
            f" font-family: '{t.MONO}'; font-weight: 700; font-size: {max(10, size // 3)}px;"
        )
        if icon:
            self.setPixmap(t.pixmap(icon, color, size // 2))
        else:
            self.setText(initials(text))


def initials(name: str) -> str:
    """'LoonVT' -> 'LV', 'loon_vt' -> 'LV', 'pepe' -> 'PE'."""
    parts = [p for p in name.replace("_", " ").replace("-", " ").split() if p]
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    clean = "".join(c for c in name if c.isalnum())
    caps = [c for c in clean if c.isupper()]
    if len(caps) >= 2:
        return caps[0] + caps[1]
    return clean[:2].upper() or "?"


# ------------------------------------------------------------------ botones
def button(text: str = "", icon: str | None = None, object_name: str = "", slot: Callable | None = None,
           icon_color: str | None = None, tooltip: str = "") -> QPushButton:
    b = QPushButton(text)
    if object_name:
        b.setObjectName(object_name)
    if icon:
        color = icon_color or ("#ffffff" if object_name == "primary" else t.TEXT_2)
        b.setIcon(t.icon(icon, color, 16))
        b.setIconSize(QSize(16, 16))
    if slot is not None:
        b.clicked.connect(slot)
    if tooltip:
        b.setToolTip(tooltip)
    b.setCursor(Qt.PointingHandCursor)
    b.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)  # nunca más estrecho que su texto
    return b


def icon_button(name: str, tooltip: str = "", slot: Callable | None = None, color: str = t.TEXT_2,
                size: int = 16, object_name: str = "iconBtn") -> QPushButton:
    b = QPushButton()
    b.setObjectName(object_name)
    b.setIcon(t.icon(name, color, size))
    b.setIconSize(QSize(size, size))
    b.setToolTip(tooltip)
    b.setCursor(Qt.PointingHandCursor)
    if slot is not None:
        b.clicked.connect(slot)
    return b


def soon(widget: QWidget, what: str = "") -> QWidget:
    """Desactiva un control de algo que todavía no existe («Próximamente»)."""
    widget.setEnabled(False)
    widget.setToolTip(f"Próximamente{': ' + what if what else ''}")
    return widget


class Toggle(QAbstractButton):
    """Interruptor tipo píldora (morado cuando está activo)."""

    def __init__(self, checked: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(42, 24)

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt)
        return QSize(42, 24)

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        on, enabled = self.isChecked(), self.isEnabled()
        track = QColor(t.ACCENT if on else t.BG_600)
        if not enabled:
            track.setAlphaF(0.4)
        p.setPen(Qt.NoPen)
        p.setBrush(track)
        r = QRectF(1, 1, self.width() - 2, self.height() - 2)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        d = r.height() - 6
        x = r.right() - d - 3 if on else r.left() + 3
        p.setBrush(QColor("#ffffff" if enabled else t.TEXT_3))
        p.drawEllipse(QRectF(x, r.top() + 3, d, d))


class SearchBox(QLineEdit):
    def __init__(self, placeholder: str = "Buscar…") -> None:
        super().__init__()
        self.setObjectName("search")
        self.setPlaceholderText(placeholder)
        self.addAction(t.icon("search", t.TEXT_3, 16), QLineEdit.LeadingPosition)
        self.setClearButtonEnabled(True)


# ------------------------------------------------------------ cabeceras
def section_header(icon: str, title: str, right: str = "", color: str = t.ACCENT_SOFT) -> tuple[QHBoxLayout, QLabel]:
    """«☆ COMANDOS PERSONALIZADOS ……… 14 comandos». Devuelve (layout, etiqueta de la derecha)."""
    right_lbl = label(right, color=t.TEXT_3, size=11, mono=True)
    lay = hbox(icon_label(icon, color, 16), label(title.upper(), "sectionTitle"), None, right_lbl, spacing=8)
    return lay, right_lbl


# -------------------------------------------------------- selectores
class ChipGroup(QWidget):
    """Botones tipo píldora de selección única (filtros, permisos…)."""

    changed = Signal(object)

    def __init__(self, options: Iterable[tuple[str, object, str | None]], spacing: int = 8,
                 stretch: bool = True) -> None:
        super().__init__()
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: dict[object, QPushButton] = {}
        lay = hbox(spacing=spacing)
        for text, data, icon in options:
            b = button(text, icon, "chip")
            b.setCheckable(True)
            self._group.addButton(b)
            self._buttons[data] = b
            b.clicked.connect(lambda _=False, d=data: self.changed.emit(d))
            lay.addWidget(b)
        if stretch:
            lay.addStretch(1)
        self.setLayout(lay)

    def value(self):
        for data, b in self._buttons.items():
            if b.isChecked():
                return data
        return None

    def set_value(self, data) -> None:
        if data in self._buttons:
            self._buttons[data].setChecked(True)

    def set_text(self, data, text: str) -> None:
        if data in self._buttons:
            self._buttons[data].setText(text)


class PlatformPicker(QWidget):
    """Tarjetas «Twitch / Kick / Ambas (Sincro)» del modal de comandos."""

    def __init__(self, selected: frozenset[Platform]) -> None:
        super().__init__()
        self._value = set(selected)
        self._twitch = self._make("Twitch", "tv", t.ACCENT)
        self._kick = self._make("Kick", "zap", t.KICK)
        self._both = self._make("Ambas (Sincro)", "circle-check", t.GREEN)
        self._twitch.clicked.connect(lambda: self._toggle(Platform.TWITCH))
        self._kick.clicked.connect(lambda: self._toggle(Platform.KICK))
        self._both.clicked.connect(self._set_both)
        self.setLayout(hbox(self._twitch, self._kick, self._both, spacing=12))
        self._refresh()

    @staticmethod
    def _make(text: str, icon: str, color: str) -> QPushButton:
        b = button(text, icon, icon_color=color)
        b.setMinimumHeight(42)
        b.setProperty("accent", color)
        return b

    def _toggle(self, p: Platform) -> None:
        self._value ^= {p}
        self._refresh()

    def _set_both(self) -> None:
        self._value = set(Platform)
        self._refresh()

    def _refresh(self) -> None:
        for b, on in ((self._twitch, Platform.TWITCH in self._value), (self._kick, Platform.KICK in self._value),
                      (self._both, self._value == set(Platform))):
            color = b.property("accent")
            if on:
                text_color = t.TEXT if color == t.ACCENT else color
                b.setStyleSheet(
                    f"QPushButton {{ background: {t.alpha(color, 0.10)}; border: 2px solid {t.alpha(color, 0.85)};"
                    f" border-radius: 10px; color: {text_color}; font-weight: 600; text-align: left;"
                    f" padding: 8px 14px; }}"
                )
            else:
                b.setStyleSheet(
                    f"QPushButton {{ background: {t.BG_800}; border: 1px solid {t.BORDER}; border-radius: 10px;"
                    f" text-align: left; padding: 8px 14px; color: {t.TEXT_2}; }}"
                    f" QPushButton:hover {{ border-color: {t.BORDER_HI}; }}"
                )

    def value(self) -> frozenset[Platform]:
        return frozenset(self._value)


def platforms_text(platforms: frozenset[Platform]) -> tuple[str, str]:
    """(texto, color) del estilo «Ambos (Twitch+Kick)» / «Solo Twitch»."""
    if platforms >= set(Platform):
        return "Ambos (Twitch+Kick)", t.GREEN
    if Platform.TWITCH in platforms:
        return "Solo Twitch", t.ACCENT
    if Platform.KICK in platforms:
        return "Solo Kick", t.KICK
    return "Ninguna plataforma", t.TEXT_3


# ------------------------------------------------------------------ tablas
def table(headers: list[str], stretch_col: int) -> QTableWidget:
    tw = QTableWidget(0, len(headers))
    tw.setHorizontalHeaderLabels(headers)
    tw.setSelectionBehavior(QAbstractItemView.SelectRows)
    tw.setSelectionMode(QAbstractItemView.SingleSelection)
    tw.setEditTriggers(QAbstractItemView.NoEditTriggers)
    tw.verticalHeader().setVisible(False)
    tw.setShowGrid(False)
    tw.setWordWrap(False)
    h = tw.horizontalHeader()
    h.setSectionResizeMode(QHeaderView.ResizeToContents)
    h.setSectionResizeMode(stretch_col, QHeaderView.Stretch)
    h.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    return tw


def fill(tw: QTableWidget, rows: list[list[str]]) -> None:
    tw.setRowCount(len(rows))
    for r, values in enumerate(rows):
        for c, v in enumerate(values):
            tw.setItem(r, c, QTableWidgetItem(v))


# -------------------------------------------------------------------- modal
class _Dim(QWidget):
    """Capa oscura sobre la ventana principal mientras hay un modal abierto."""

    def __init__(self, host: QWidget) -> None:
        super().__init__(host)
        self.setGeometry(host.rect())
        host.installEventFilter(self)

    def eventFilter(self, obj: QObject, ev: QEvent) -> bool:  # noqa: N802 (Qt)
        if ev.type() == QEvent.Resize:
            self.setGeometry(obj.rect())
        return False

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt)
        QPainter(self).fillRect(self.rect(), QColor(5, 6, 8, 170))


class ModalDialog(QDialog):
    """Ventana modal con el estilo del diseño: cabecera con icono, cuerpo y pie.

    Se abre con ``open()`` (nunca ``exec()``, que frena qasync). Las subclases
    añaden sus controles a ``self.body`` y sus botones a ``self.footer``.
    """

    def __init__(self, title: str, subtitle: str = "", icon: str = "terminal", parent=None,
                 tag: str = "", width: int = 760) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle(title)
        self._dim: _Dim | None = None

        card = frame("modalCard")
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(60)
        shadow.setOffset(0, 12)
        shadow.setColor(QColor(145, 70, 255, 70))
        card.setGraphicsEffect(shadow)

        icon_box = QLabel()
        icon_box.setFixedSize(40, 40)
        icon_box.setAlignment(Qt.AlignCenter)
        icon_box.setPixmap(t.pixmap(icon, "#ffffff", 20))
        icon_box.setStyleSheet(f"background: {t.ACCENT}; border-radius: 10px;")
        self.title_label = label(title, size=17, bold=True)
        title_row = hbox(self.title_label, spacing=10)
        if tag:
            title_row.addWidget(Pill(tag, t.ACCENT_SOFT, size=11, bold=False))
        title_row.addStretch(1)
        texts = vbox(title_row, spacing=3)
        if subtitle:
            texts.addWidget(label(subtitle, color=t.TEXT_2, size=13, wrap=True))
        close = icon_button("x", "Cerrar (Esc)", self.reject, t.TEXT_2, 16, "iconBox")
        head = hbox(icon_box, texts, close, spacing=14, margins=(24, 20, 20, 18))
        head.setStretchFactor(texts, 1)
        header = frame("modalHeader", head)

        self.body = vbox(spacing=14, margins=(24, 18, 24, 18))
        body_w = QWidget()
        body_w.setLayout(self.body)
        self.footer = hbox(spacing=10, margins=(24, 14, 24, 16))
        footer = frame("modalFooter", self.footer)

        card.setLayout(vbox(header, body_w, footer, spacing=0))
        self.setLayout(vbox(card, margins=(30, 30, 30, 40)))
        self.setMinimumWidth(width)

    @staticmethod
    def field_label(text: str, required: bool = False, hint: str = "") -> QHBoxLayout:
        star = f" <span style='color:{t.ACCENT}'>*</span>" if required else ""
        lay = hbox(QLabel(f"<b>{text}</b>{star}"), None, spacing=6)
        if hint:
            lay.addWidget(label(hint, color=t.TEXT_3, size=11, mono=True))
        return lay

    def showEvent(self, event) -> None:  # noqa: N802 (Qt)
        host = self.parentWidget().window() if self.parentWidget() else None
        if host is not None and self._dim is None:
            self._dim = _Dim(host)
            self._dim.show()
            self._dim.raise_()
        super().showEvent(event)
        if host is not None:
            self.adjustSize()
            geo = host.geometry()
            self.move(geo.center().x() - self.width() // 2,
                      max(geo.top() + 20, geo.center().y() - self.height() // 2))

    def done(self, result: int) -> None:
        if self._dim is not None:
            self._dim.deleteLater()
            self._dim = None
        super().done(result)


# ------------------------------------------------------- utilidades varias
def fmt_mmss(seconds: float | int | None) -> str:
    if seconds is None or seconds < 0:
        return "--:--"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def draw_dot(p: QPainter, x: float, y: float, color: str, r: float = 3.5) -> None:
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    p.drawEllipse(QPointF(x, y), r, r)


def pen(color: str, width: float = 1.0, a: float = 1.0) -> QPen:
    pn = QPen(t.qcolor(color, a))
    pn.setWidthF(width)
    return pn
