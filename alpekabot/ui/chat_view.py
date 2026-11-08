"""Chat unificado con tarjetas (avatar, insignias, mensajes del bot, tarjetas de canción y avisos).

Se dibuja con un ``QListView`` + delegado propio: el texto enriquecido de Qt no
admite esquinas redondeadas y así va fluido con 1000 mensajes.
"""

from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from PySide6.QtCore import QAbstractListModel, QModelIndex, QPoint, QRectF, QSize, QSortFilterProxyModel, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAbstractTextDocumentLayout,
    QColor,
    QFontMetrics,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QTextDocument,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QLineEdit,
    QListView,
    QMenu,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QWidget,
)

from ..core.models import ChatMessage, Platform
from . import theme as t
from .widgets import button, frame, hbox, initials, vbox

MAX_ENTRIES = 1000
ECHO_WINDOW_S = 20  # el eco de un mensaje del bot sustituido por una tarjeta se ignora durante este tiempo

_ROLE_PILLS = {
    "broadcaster": ("Broadcaster", t.GOLD),
    "moderator": ("MOD", t.BLUE),
    "vip": ("VIP", t.PINK),
    "subscriber": ("Sub", t.ACCENT_SOFT),
    "founder": ("Founder", t.ACCENT_SOFT),
}
_PLATFORM_PILL = {Platform.TWITCH: ("Twitch", "tv", t.ACCENT_SOFT), Platform.KICK: ("Kick", "zap", t.KICK)}
_SYSTEM_STYLE = {  # nivel -> (icono, color)
    "ok": ("circle-check", t.GREEN),
    "info": ("info", t.TEXT_2),
    "error": ("circle-x", t.RED),
    "mod": ("shield", t.ORANGE),
}
_URL_RE = re.compile(r"(https?://[^\s<]+|www\.[^\s<]+)")
_MENTION_RE = re.compile(r"(?<![\w@])@(\w{2,25})")


@dataclass
class ChatEntry:
    kind: str                       # user | bot | song | system | mod
    msg: ChatMessage | None = None
    text: str = ""
    level: str = "info"
    when: datetime = field(default_factory=datetime.now)
    song: dict | None = None        # title, duration, position, playing
    heights: dict = field(default_factory=dict)  # ancho -> alto (caché)

    @property
    def search_text(self) -> str:
        if self.msg is not None:
            return f"{self.msg.user_name} {self.msg.text}"
        if self.song:
            return f"{self.song.get('title', '')} {self.text}"
        return self.text


# ------------------------------------------------------------------- modelo
class ChatModel(QAbstractListModel):
    EntryRole = Qt.UserRole + 1

    def __init__(self) -> None:
        super().__init__()
        self.entries: list[ChatEntry] = []

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802 (Qt)
        return 0 if parent.isValid() else len(self.entries)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        e = self.entries[index.row()]
        if role == self.EntryRole:
            return e
        if role == Qt.DisplayRole:
            return e.search_text
        return None

    def append(self, entry: ChatEntry) -> None:
        if len(self.entries) >= MAX_ENTRIES:
            drop = len(self.entries) - MAX_ENTRIES + 1
            self.beginRemoveRows(QModelIndex(), 0, drop - 1)
            del self.entries[:drop]
            self.endRemoveRows()
        n = len(self.entries)
        self.beginInsertRows(QModelIndex(), n, n)
        self.entries.append(entry)
        self.endInsertRows()

    def clear(self) -> None:
        self.beginResetModel()
        self.entries.clear()
        self.endResetModel()


# ------------------------------------------------------------------ dibujo
def _body_html(text: str, command: bool = False) -> str:
    """Texto escapado con enlaces en azul y menciones en morado (sin enlaces reales)."""
    esc = html.escape(text)
    esc = _URL_RE.sub(lambda m: f'<span style="color:{t.BLUE}">{m.group(1)}</span>', esc)
    esc = _MENTION_RE.sub(lambda m: f'<span style="color:{t.ACCENT_SOFT}">@{m.group(1)}</span>', esc)
    if command:
        esc = re.sub(r"^(!\w+)", lambda m: f'<span style="color:{t.ACCENT_SOFT}">{m.group(1)}</span>', esc)
    return f'<span style="color:{t.TEXT}">{esc}</span>'


def _doc(html_text: str, width: float, mono: bool = False, size: int = 14) -> QTextDocument:
    doc = QTextDocument()
    doc.setDocumentMargin(0)
    doc.setDefaultFont(t.font(size - (1 if mono else 0), mono=mono))
    doc.setHtml(html_text)
    doc.setTextWidth(max(50.0, width))
    return doc


def _pill(p: QPainter, x: float, y: float, text: str, color: str, icon: str | None = None,
          size: int = 11, h: float = 20, bold: bool = True, mono: bool = True) -> float:
    """Dibuja una insignia y devuelve su ancho."""
    f = t.font(size, t.QFont.Medium if bold else t.QFont.Normal, mono=mono)
    fm = QFontMetrics(f)
    pad = 7
    icon_w = (size + 4) if icon else 0
    w = pad * 2 + icon_w + fm.horizontalAdvance(text)
    r = QRectF(x, y, w, h)
    p.setPen(t.qcolor(color, 0.45))
    p.setBrush(t.qcolor(color, 0.12))
    p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 5, 5)
    tx = x + pad
    if icon:
        p.drawPixmap(int(tx), int(y + (h - size) / 2), t.pixmap(icon, color, size))
        tx += icon_w
    p.setFont(f)
    p.setPen(QColor(color))
    p.drawText(QRectF(tx, y, w, h), Qt.AlignVCenter | Qt.AlignLeft, text)
    return w


def _rounded(p: QPainter, r: QRectF, radius: float, fill: QColor, border: QColor | None = None) -> None:
    path = QPainterPath()
    path.addRoundedRect(r, radius, radius)
    p.fillPath(path, fill)
    if border is not None:
        p.setPen(border)
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)


class ChatDelegate(QStyledItemDelegate):
    MARGIN_X = 20
    MARGIN_Y = 8
    AVATAR = 40
    GAP = 14
    HEADER_H = 22
    CARD_PAD = 14

    def _is_card(self, e: ChatEntry) -> bool:
        return e.kind in ("bot", "song", "system", "mod")

    # --------------------------------------------------------------- tamaños
    def _text_width(self, e: ChatEntry, total: float) -> float:
        pad = self.CARD_PAD if self._is_card(e) else 0
        return total - 2 * self.MARGIN_X - 2 * pad - self.AVATAR - self.GAP

    def _body_height(self, e: ChatEntry, text_w: float) -> float:
        if e.kind == "song":
            return 74
        if e.kind == "system":
            return _doc(self._system_html(e), text_w, mono=True, size=13).size().height()
        command = e.kind == "user" and e.msg.text.startswith("!")
        if command:
            return _doc(_body_html(e.msg.text, True), text_w - 26, mono=True).size().height() + 18
        text = e.msg.text if e.msg else e.text
        return _doc(_body_html(text), text_w).size().height()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802 (Qt)
        e: ChatEntry = index.data(ChatModel.EntryRole)
        width = option.rect.width() or 800
        cached = e.heights.get(width)
        if cached is None:
            text_w = self._text_width(e, width)
            body = self._body_height(e, text_w)
            pad = self.CARD_PAD if self._is_card(e) else 0
            header = 0 if e.kind == "system" else self.HEADER_H + 8
            content = max(self.AVATAR if e.kind != "system" else 36, header + body)
            cached = int(content + 2 * pad + 2 * self.MARGIN_Y)
            e.heights.clear()
            e.heights[width] = cached
        return QSize(width, cached)

    # ---------------------------------------------------------------- pintar
    def paint(self, p: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        e: ChatEntry = index.data(ChatModel.EntryRole)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        r = QRectF(option.rect)
        card = r.adjusted(self.MARGIN_X, self.MARGIN_Y, -self.MARGIN_X, -self.MARGIN_Y)
        pad = self.CARD_PAD if self._is_card(e) else 0
        if e.kind in ("bot", "song"):
            _rounded(p, card, 14, t.qcolor(t.ACCENT, 0.05), t.qcolor(t.ACCENT, 0.30))
        elif e.kind == "mod":
            _rounded(p, card, 14, t.qcolor(t.ORANGE, 0.05), t.qcolor(t.ORANGE, 0.30))
        elif e.kind == "system":
            color = _SYSTEM_STYLE.get(e.level, _SYSTEM_STYLE["info"])[1]
            _rounded(p, card, 12, t.qcolor(color, 0.05) if e.level != "info" else QColor(t.BG_850),
                     t.qcolor(color, 0.30) if e.level != "info" else QColor(t.BORDER))
        x0, y0 = card.left() + pad, card.top() + pad
        text_x = x0 + self.AVATAR + self.GAP
        text_w = card.right() - pad - text_x
        if e.kind == "system":
            self._paint_system(p, e, x0, y0, text_x, text_w)
        else:
            self._paint_avatar(p, e, x0, y0)
            self._paint_header(p, e, text_x, y0)
            by = y0 + self.HEADER_H + 8
            if e.kind == "song":
                self._paint_song(p, e, text_x, by, min(text_w, 720))
            else:
                self._paint_body(p, e, text_x, by, text_w)
        p.restore()

    def _paint_avatar(self, p: QPainter, e: ChatEntry, x: float, y: float) -> None:
        r = QRectF(x, y, self.AVATAR, self.AVATAR)
        if e.kind in ("bot", "song"):
            _rounded(p, r, 10, QColor(t.ACCENT))
            p.drawPixmap(int(x + 10), int(y + 10), t.pixmap("sparkles", "#ffffff", 20))
        elif e.kind == "mod":
            _rounded(p, r, 10, t.qcolor(t.ORANGE, 0.15), t.qcolor(t.ORANGE, 0.5))
            p.drawPixmap(int(x + 10), int(y + 10), t.pixmap("shield", t.ORANGE, 20))
        else:
            _rounded(p, r.adjusted(0.5, 0.5, -0.5, -0.5), 10, QColor(t.BG_800), QColor(t.BORDER_HI))
            p.setFont(t.font(14, t.QFont.Bold, mono=True))
            p.setPen(QColor(t.TEXT))
            p.drawText(r, Qt.AlignCenter, initials(e.msg.user_name if e.msg else "?"))

    def _paint_header(self, p: QPainter, e: ChatEntry, x: float, y: float) -> None:
        msg = e.msg
        platform = msg.platform if msg else (e.song or {}).get("platform")
        if platform in _PLATFORM_PILL:
            name, icon, color = _PLATFORM_PILL[platform]
            x += _pill(p, x, y, name, color, icon) + 8
        if e.kind in ("bot", "song"):
            x += _pill(p, x, y, msg.user_name if msg else "AlpekaBot", t.ACCENT_SOFT) + 8
            x += _pill(p, x, y + 2, "BOT", t.GREEN, size=9, h=16) + 10
        elif e.kind == "mod":
            x += _pill(p, x, y, "Moderación", t.ORANGE, "shield") + 10
        else:
            for b in msg.badges:
                if b in _ROLE_PILLS:
                    text, color = _ROLE_PILLS[b]
                    x += _pill(p, x, y, text, color) + 8
            p.setFont(t.font(14, t.QFont.DemiBold))
            color = msg.color or t.TEXT
            p.setPen(QColor(color) if QColor(color).lightness() > 90 else QColor(t.TEXT))
            fm = QFontMetrics(p.font())
            p.drawText(QRectF(x, y, fm.horizontalAdvance(msg.user_name) + 4, 20), Qt.AlignVCenter, msg.user_name)
            x += fm.horizontalAdvance(msg.user_name) + 12
        p.setFont(t.font(11, mono=True))
        p.setPen(QColor(t.TEXT_3))
        p.drawText(QRectF(x, y, 90, 20), Qt.AlignVCenter, e.when.strftime("%H:%M:%S"))

    def _paint_body(self, p: QPainter, e: ChatEntry, x: float, y: float, w: float) -> None:
        text = e.msg.text if e.msg else e.text
        if e.kind == "user" and text.startswith("!"):
            doc = _doc(_body_html(text, True), w - 26, mono=True)
            box = QRectF(x, y, min(w, doc.idealWidth() + 26), doc.size().height() + 18)
            _rounded(p, box.adjusted(0.5, 0.5, -0.5, -0.5), 8, QColor(t.BG_850), QColor(t.BORDER))
            self._draw_doc(p, doc, x + 13, y + 9)
        else:
            self._draw_doc(p, _doc(_body_html(text), w), x, y)

    @staticmethod
    def _draw_doc(p: QPainter, doc: QTextDocument, x: float, y: float) -> None:
        p.save()
        p.translate(x, y)
        ctx = QAbstractTextDocumentLayout.PaintContext()
        ctx.palette.setColor(ctx.palette.ColorRole.Text, QColor(t.TEXT))
        doc.documentLayout().draw(p, ctx)
        p.restore()

    def _paint_song(self, p: QPainter, e: ChatEntry, x: float, y: float, w: float) -> None:
        s = e.song or {}
        box = QRectF(x, y, w, 74)
        _rounded(p, box.adjusted(0.5, 0.5, -0.5, -0.5), 12, QColor(t.BG_900), QColor(t.BORDER))
        ic = QRectF(x + 14, y + 13, 48, 48)
        _rounded(p, ic, 10, QColor(t.BG_800), QColor(t.BORDER))
        p.drawPixmap(int(ic.left() + 12), int(ic.top() + 12), t.pixmap("music", t.ACCENT_SOFT, 24))
        playing = s.get("playing")
        pill_text = "SONANDO" if playing else "LISTA"
        pill_w = QFontMetrics(t.font(11, t.QFont.DemiBold, mono=True)).horizontalAdvance(pill_text) + 14
        tx, tw = x + 78, w - 78 - pill_w - 30
        p.setFont(t.font(14, t.QFont.DemiBold))
        p.setPen(QColor(t.TEXT))
        title = QFontMetrics(p.font()).elidedText(s.get("title", ""), Qt.ElideRight, int(tw))
        p.drawText(QRectF(tx, y + 14, tw, 22), Qt.AlignVCenter, title)
        p.setFont(t.font(12, mono=True))
        meta_y = y + 40
        p.setPen(QColor(t.TEXT_3))
        dur = f"Duración: {s.get('duration', '--:--')}"
        p.drawText(QRectF(tx, meta_y, tw, 20), Qt.AlignVCenter, dur)
        dx = tx + QFontMetrics(p.font()).horizontalAdvance(dur) + 10
        p.drawText(QRectF(dx, meta_y, 14, 20), Qt.AlignVCenter, "·")
        p.setPen(QColor(t.ACCENT_SOFT))
        pos = "Sonando ahora" if playing else f"Posición en cola: #{s.get('position', '?')}"
        p.drawText(QRectF(dx + 16, meta_y, tw, 20), Qt.AlignVCenter, pos)
        _pill(p, box.right() - pill_w - 14, y + 26, pill_text, t.GREEN if playing else t.ACCENT_SOFT)

    @staticmethod
    def _system_html(e: ChatEntry) -> str:
        return (f'<span style="color:{t.TEXT_3}">{e.when:%H:%M:%S} · </span>'
                f'<span style="color:{t.TEXT}">{html.escape(e.text)}</span>')

    def _paint_system(self, p: QPainter, e: ChatEntry, x: float, y: float, text_x: float, text_w: float) -> None:
        icon, color = _SYSTEM_STYLE.get(e.level, _SYSTEM_STYLE["info"])
        r = QRectF(x, y, 36, 36)
        _rounded(p, r, 9, t.qcolor(color, 0.10), t.qcolor(color, 0.4))
        p.drawPixmap(int(x + 9), int(y + 9), t.pixmap(icon, color, 18))
        doc = _doc(self._system_html(e), text_w, mono=True, size=13)
        self._draw_doc(p, doc, text_x, y + max(0.0, (36 - doc.size().height()) / 2))


# --------------------------------------------------------------------- vista
class ChatView(QWidget):
    send_requested = Signal(object, str)  # (Platform o None = todas, texto)
    # (acción, ChatMessage): delete, timeout:<segundos>, timeout:custom, ban, unban, permit
    moderation_requested = Signal(str, object)
    count_changed = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("page")
        self._is_bot: Callable[[ChatMessage], bool] = lambda _m: False
        self._echo: dict[tuple[Platform, str], float] = {}
        self._count = 0

        self.model = ChatModel()
        self._proxy = QSortFilterProxyModel(self)
        self._proxy.setSourceModel(self.model)
        self._proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self._list = QListView()
        self._list.setModel(self._proxy)
        self._list.setItemDelegate(ChatDelegate(self._list))
        self._list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self._list.verticalScrollBar().setSingleStep(24)
        self._list.setSelectionMode(QAbstractItemView.NoSelection)
        self._list.setResizeMode(QListView.Adjust)
        self._list.setUniformItemSizes(False)
        self._list.setFocusPolicy(Qt.NoFocus)
        self._list.setContextMenuPolicy(Qt.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._show_menu)
        self._list.setStyleSheet(f"QListView {{ background: {t.BG_900}; padding-top: 8px; }}")
        self._proxy.rowsInserted.connect(self._maybe_scroll)

        # ------------------------------------------------ caja de envío
        self._target = QComboBox()
        self._target.setMinimumWidth(220)
        self._target.setToolTip("Plataforma donde se enviará el mensaje")
        self._input = QLineEdit()
        self._input.setObjectName("composer")
        self._input.setPlaceholderText("Escribe un mensaje como el bot…")
        self._input.setMaxLength(500)
        self._input.addAction(t.icon("smile", t.TEXT_3, 18), QLineEdit.TrailingPosition)
        self._input.returnPressed.connect(self._emit_send)
        self._send = button("Enviar", "send", "primary", self._emit_send)
        self._send.setLayoutDirection(Qt.RightToLeft)  # icono a la derecha, como en el diseño
        self._send.setMinimumHeight(44)
        self._target.setMinimumHeight(44)
        composer = frame("composerBar", hbox(self._target, self._input, self._send, spacing=12,
                                             margins=(16, 14, 16, 14)))
        composer.setStyleSheet(f"QFrame#composerBar {{ background: {t.BG_900}; border-top: 1px solid {t.BORDER}; }}")

        self.setLayout(vbox(self._list, composer, spacing=0))
        self.set_input_enabled(False)

    # ------------------------------------------------------------- públicos
    def set_bot_check(self, is_bot: Callable[[ChatMessage], bool]) -> None:
        self._is_bot = is_bot

    def set_platforms(self, platforms: list[Platform]) -> None:
        """Plataformas conectadas a las que se puede escribir (con «Ambas» si hay varias)."""
        current = self._target.currentData()
        self._target.blockSignals(True)
        self._target.clear()
        if len(platforms) > 1:
            self._target.addItem(t.icon("radio", t.ACCENT_SOFT), "Twitch + Kick (Ambos)", None)
        for p in platforms:
            color = t.PLATFORM_COLORS[p.value]
            self._target.addItem(t.icon("tv" if p is Platform.TWITCH else "zap", color), p.label, p)
        if not platforms:
            self._target.addItem(t.icon("plug", t.TEXT_3), "Sin conexión", None)
        idx = self._target.findData(current)
        self._target.setCurrentIndex(idx if idx >= 0 else 0)
        self._target.blockSignals(False)
        self.set_input_enabled(bool(platforms))

    def set_input_enabled(self, enabled: bool) -> None:
        for w in (self._input, self._send, self._target):
            w.setEnabled(enabled)

    def clear_input(self) -> None:
        self._input.clear()

    def set_filter(self, text: str) -> None:
        self._proxy.setFilterFixedString(text.strip())

    def clear(self) -> None:
        self.model.clear()

    @property
    def message_count(self) -> int:
        return self._count

    def add_message(self, msg: ChatMessage) -> None:
        if self._is_bot(msg):
            key = (msg.platform, msg.text.strip())
            seen = self._echo.pop(key, None)
            if seen is not None and time.monotonic() - seen < ECHO_WINDOW_S:
                return  # ya se mostró como tarjeta de canción
            self.model.append(ChatEntry("bot", msg, when=msg.timestamp))
        else:
            self._count += 1
            self.count_changed.emit(self._count)
            self.model.append(ChatEntry("user", msg, when=msg.timestamp))

    def add_song_card(self, platform: Platform, bot_name: str, title: str, duration: str,
                      position: int, playing: bool, reply_text: str) -> None:
        """Tarjeta «canción añadida». El mensaje de texto equivalente del bot no se repite."""
        now = time.monotonic()
        self._echo = {k: v for k, v in self._echo.items() if now - v < ECHO_WINDOW_S}
        self._echo[(platform, reply_text.strip())] = now
        fake = ChatMessage(platform, "", "", "", bot_name.lower(), bot_name, reply_text)
        self.model.append(ChatEntry("song", fake, song={
            "title": title, "duration": duration, "position": position, "playing": playing, "platform": platform,
        }))

    def add_system(self, text: str, kind: str = "info") -> None:
        self.model.append(ChatEntry("mod" if kind == "mod" else "system", text=text, level=kind))

    def add_mod_action(self, platform: Platform, text: str) -> None:
        fake = ChatMessage(platform, "", "", "", "", "Moderación", text)
        self.model.append(ChatEntry("mod", fake, text=text, level="mod"))

    # ------------------------------------------------------- menú de moderación
    def message_at(self, pos: QPoint) -> ChatMessage | None:
        index = self._list.indexAt(pos)
        if not index.isValid():
            return None
        e: ChatEntry = index.data(ChatModel.EntryRole)
        return e.msg if e.kind == "user" else None

    def _show_menu(self, pos: QPoint) -> None:
        msg = self.message_at(pos)
        if msg is None:
            return
        menu = QMenu(self)
        title = menu.addAction(t.icon("user", t.TEXT_3), f"{msg.user_name} ({msg.platform.label})")
        title.setEnabled(False)
        menu.addSeparator()
        copy = menu.addAction(t.icon("copy"), "Copiar mensaje")
        copy.triggered.connect(lambda: _copy(msg.text))
        menu.addSeparator()
        protected = msg.is_moderator
        for icon, text, action, allowed in (
            ("trash-2", "Borrar mensaje", "delete", True),
            ("timer", "Timeout 1 min", "timeout:60", not protected),
            ("timer", "Timeout 10 min", "timeout:600", not protected),
            ("clock", "Timeout…", "timeout:custom", not protected),
            ("ban", "Banear…", "ban", not protected),
            ("circle-check", "Desbanear / quitar timeout", "unban", not protected),
            ("link", "Permitir un enlace (!permit)", "permit", not protected),
        ):
            act = menu.addAction(t.icon(icon, t.RED if action == "ban" else t.TEXT_2), text)
            act.setEnabled(allowed)
            act.triggered.connect(lambda _=False, a=action, m=msg: self.moderation_requested.emit(a, m))
        menu.setAttribute(Qt.WA_DeleteOnClose)
        menu.popup(self._list.viewport().mapToGlobal(pos))  # popup y no exec: sin bucle anidado

    # ------------------------------------------------------------- internos
    def _maybe_scroll(self, *_args) -> None:
        bar = self._list.verticalScrollBar()
        if bar.value() >= bar.maximum() - 60:  # no saltar al final si el usuario está leyendo arriba
            QTimer.singleShot(0, self._list.scrollToBottom)

    def _emit_send(self) -> None:
        text = self._input.text().strip()
        if text:
            self.send_requested.emit(self._target.currentData(), text)


def _copy(text: str) -> None:
    QGuiApplication.clipboard().setText(text)
