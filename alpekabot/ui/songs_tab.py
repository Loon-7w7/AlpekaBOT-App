"""Página «Canciones & Song Requests»: reproductor, cola, reglas, respaldo y lista negra."""

from __future__ import annotations

import asyncio
import logging
import re

import shiboken6

from PySide6.QtCore import QEasingCurve, QEvent, QRectF, QSize, Qt, QUrl, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QWidget,
)

from ..core.models import Permission, Platform
from ..features.songs import BLACKLIST_KINDS, Song, SongService, SongStatus
from ..features.youtube import YouTubeError, format_duration
from . import theme as t
from .commands_tab import ElidedLabel
from .widgets import (
    ChipGroup,
    ModalDialog,
    Pill,
    SearchBox,
    Toggle,
    button,
    clear_layout,
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
from .youtube_player import WebPlayer

log = logging.getLogger(__name__)

_SOURCE = {Platform.TWITCH: ("TWITCH", t.ACCENT_SOFT), Platform.KICK: ("KICK", t.KICK)}


def source_box(text: str = "YT", color: str = t.RED, size: int = 44) -> QLabel:
    box = QLabel(text)
    box.setFixedSize(size, size)
    box.setAlignment(Qt.AlignCenter)
    box.setStyleSheet(f"background: {t.BG_900}; border: 1px solid {t.BORDER}; border-radius: 8px; color: {color};"
                      f" font-family: '{t.MONO}'; font-weight: 700; font-size: 11px;")
    return box


_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
ART_SIZE = 110  # cuadro de la portada (o el disco) en la tarjeta principal
BAN_SIZE, BAN_MARGIN = 28, 10  # botón de bloquear: esquina superior derecha de la tarjeta


class Thumbnails:
    """Miniaturas de YouTube para las filas (``i.ytimg.com``, sin clave). Se descargan una vez por
    sesión; mientras llegan, o si fallan, la fila enseña las iniciales del título."""

    _net: QNetworkAccessManager | None = None
    _cache: dict[tuple[str, int, int], QPixmap] = {}
    _waiting: dict[str, list[tuple[QLabel, int, int]]] = {}
    _raw: dict[str, QPixmap] = {}

    @classmethod
    def label(cls, video_id: str, title: str, size: int = 44) -> QLabel:
        box = source_box(initials(title), t.TEXT, size)
        cls.load(box, video_id, size)
        return box

    @classmethod
    def load(cls, box: QLabel, video_id: str, size: int, radius: int = 8) -> bool:
        """Pone la miniatura en ``box`` (al momento o cuando llegue). False si el ID no es válido.

        Si se pide otra miniatura para la misma etiqueta antes de que llegue, gana la última."""
        if not _VIDEO_ID.fullmatch(video_id or ""):
            return False
        box.setProperty("thumb_id", video_id)
        if video_id in cls._raw:
            cls._apply(box, video_id, size, radius)
        else:
            first = video_id not in cls._waiting
            cls._waiting.setdefault(video_id, []).append((box, size, radius))
            if first:
                cls._fetch(video_id)
        return True

    @classmethod
    def _fetch(cls, video_id: str) -> None:
        if cls._net is None:
            cls._net = QNetworkAccessManager()
        reply = cls._net.get(QNetworkRequest(QUrl(f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg")))
        reply.finished.connect(lambda: cls._done(video_id, reply))

    @classmethod
    def _done(cls, video_id: str, reply: QNetworkReply) -> None:
        waiting = cls._waiting.pop(video_id, [])
        pix = QPixmap()
        if reply.error() == QNetworkReply.NoError and pix.loadFromData(reply.readAll()):
            cls._raw[video_id] = pix
            for box, size, radius in waiting:
                # la fila pudo rehacerse, o la etiqueta pidió otra miniatura, mientras tanto
                if shiboken6.isValid(box) and box.property("thumb_id") == video_id:
                    cls._apply(box, video_id, size, radius)
        reply.deleteLater()

    @classmethod
    def _apply(cls, box: QLabel, video_id: str, size: int, radius: int = 8) -> None:
        key = (video_id, size, radius)
        if key not in cls._cache:
            src = cls._raw[video_id]
            side = min(src.width(), src.height())  # recorte cuadrado del centro
            square = src.copy((src.width() - side) // 2, (src.height() - side) // 2, side, side)
            square = square.scaled(size, size, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            out = QPixmap(size, size)
            out.fill(Qt.transparent)
            p = QPainter(out)
            p.setRenderHint(QPainter.Antialiasing)
            path = QPainterPath()
            path.addRoundedRect(QRectF(0, 0, size, size), radius, radius)
            p.setClipPath(path)
            p.drawPixmap(0, 0, square)
            p.end()
            cls._cache[key] = out
        box.setText("")
        box.setStyleSheet("background: transparent; border: none;")
        box.setPixmap(cls._cache[key])


def eta_text(seconds: int) -> str:
    m, s = divmod(max(0, int(seconds)), 60)
    return f"En ~{m}m {s:02d}s" if m else f"En ~{s}s"


def set_star(btn: QPushButton, favorite: bool, size: int = 16) -> None:
    """Pone la estrella rellena si la canción está en el respaldo, y su tooltip."""
    btn.setIcon(t.icon("star-filled" if favorite else "star", t.AMBER if favorite else t.TEXT_2, size))
    btn.setToolTip("Quitar de favoritos (sale de la playlist de respaldo)" if favorite
                   else "Añadir a favoritos (pasa a la playlist de respaldo)")


class SeekBar(QWidget):
    """Barra de progreso para adelantar: clic o arrastrar. Se envía al soltar."""

    TRACK_H = 6
    seeked = Signal(float)    # posición elegida, de 0 a 1
    scrubbed = Signal(float)  # posición mientras se arrastra, de 0 a 1

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(18)  # más alta que la línea que se pinta: así es fácil acertar
        self.setMinimumWidth(60)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setAttribute(Qt.WA_Hover)
        self.value = 0.0
        self.dragging = False
        self._seekable = False

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt)
        return QSize(200, 18)

    def set_seekable(self, seekable: bool) -> None:
        self._seekable = seekable
        self.setCursor(Qt.PointingHandCursor if seekable else Qt.ArrowCursor)
        self.setToolTip("Haz clic o arrastra para adelantar" if seekable else "")
        if not seekable:
            self.dragging = False
        self.update()

    def set_value(self, value: float) -> None:
        if not self.dragging:
            self.value = max(0.0, min(1.0, value))
            self.update()

    def _track(self, event) -> None:
        self.value = max(0.0, min(1.0, event.position().x() / max(1, self.width())))
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt)
        if self._seekable and event.button() == Qt.LeftButton:
            self.dragging = True
            self._track(event)
            self.scrubbed.emit(self.value)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt)
        if self.dragging:
            self._track(event)
            self.scrubbed.emit(self.value)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt)
        if self.dragging and event.button() == Qt.LeftButton:
            self._track(event)
            self.dragging = False
            self.seeked.emit(self.value)

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        top = (self.height() - self.TRACK_H) / 2
        radius = self.TRACK_H / 2
        p.setBrush(QColor(t.BG_600))
        p.drawRoundedRect(QRectF(0, top, self.width(), self.TRACK_H), radius, radius)
        x = self.value * self.width()
        if x > 0:
            p.setBrush(QColor(t.ACCENT))
            p.drawRoundedRect(QRectF(0, top, x, self.TRACK_H), radius, radius)
        if self._seekable and (self.dragging or self.underMouse()):
            p.setBrush(QColor("#ffffff"))
            handle = max(6.0, min(self.width() - 6.0, x))
            p.drawEllipse(QRectF(handle - 6, self.height() / 2 - 6, 12, 12))


class QueueRow(QFrame):
    def __init__(self, page: "SongsTab", song: Song, index: int, eta_s: int, favorite: bool = False) -> None:
        super().__init__()
        self.setObjectName("row")
        title = ElidedLabel(song.title)
        title.setStyleSheet("font-size: 14px; font-weight: 600;")
        title_row = hbox(title, label(format_duration(song.duration_s), color=t.TEXT_3, size=11, mono=True),
                         spacing=8)
        title_row.setStretchFactor(title, 1)
        meta = hbox(label(f"Pedida por: <b style='color:{t.TEXT}'>{song.requested_by}</b>", color=t.TEXT_3,
                          size=11, mono=True), spacing=8)
        if song.platform in _SOURCE:
            text, color = _SOURCE[song.platform]
            meta.addWidget(Pill(text, color, size=9))
        meta.addWidget(label(f"·  {eta_text(eta_s)}", color=t.TEXT_3, size=11, mono=True))
        meta.addStretch(1)
        info = QWidget()
        info.setLayout(vbox(title_row, meta, spacing=4))
        repo = page.svc.repo
        self.fav_btn = icon_button("star", "", lambda: page.svc.toggle_favorite(song), t.TEXT_2, 16)
        set_star(self.fav_btn, favorite)
        lay = hbox(label(f"#{index}", size=14, bold=True, mono=True, color=t.ACCENT_SOFT), 4,
                   Thumbnails.label(song.video_id, song.title), 6, info,
                   self.fav_btn,
                   icon_button("play", "Reproducir ya (la que suena vuelve a la cola)",
                               lambda: page.svc.play_now(song.id), t.ACCENT_SOFT, 16, "iconBox"),
                   icon_button("chevron-up", "Subir", lambda: repo.queue_move(song.id, -1), t.TEXT_2, 16),
                   icon_button("chevron-down", "Bajar", lambda: repo.queue_move(song.id, 1), t.TEXT_2, 16),
                   icon_button("trash-2", "Quitar de la cola", lambda: page.svc.remove(song.id), t.TEXT_2, 16),
                   spacing=8, margins=(16, 12, 14, 12))
        lay.setStretchFactor(info, 1)
        self.setLayout(lay)


class BackupRow(QFrame):
    """Fila de la playlist de respaldo (misma forma que la de la cola)."""

    def __init__(self, page: "SongsTab", song: Song, index: int, playing: bool) -> None:
        super().__init__()
        self.setObjectName("row")
        if playing:
            self.setStyleSheet(f"QFrame#row {{ border-color: {t.ACCENT}; background: {t.BG_800}; }}")
        title = ElidedLabel(song.title)
        title.setStyleSheet("font-size: 14px; font-weight: 600;")
        title_row = hbox(title, label(format_duration(song.duration_s), color=t.TEXT_3, size=11, mono=True),
                         spacing=8)
        title_row.setStretchFactor(title, 1)
        meta = hbox(label(song.channel or "YouTube", color=t.TEXT_3, size=11, mono=True), spacing=8)
        if playing:
            meta.addWidget(Pill("SONANDO", t.GREEN, size=9))
        meta.addStretch(1)
        info = QWidget()
        info.setLayout(vbox(title_row, meta, spacing=4))
        svc, repo = page.svc, page.svc.repo
        lay = hbox(label(f"#{index}", size=14, bold=True, mono=True, color=t.ACCENT_SOFT), 4,
                   Thumbnails.label(song.video_id, song.title), 6, info,
                   icon_button("play", "Reproducir ya", lambda: svc.play_backup(song.id), t.ACCENT_SOFT, 16, "iconBox"),
                   icon_button("chevron-up", "Subir", lambda: repo.backup_move(song.id, -1), t.TEXT_2, 16),
                   icon_button("chevron-down", "Bajar", lambda: repo.backup_move(song.id, 1), t.TEXT_2, 16),
                   icon_button("trash-2", "Quitar del respaldo", lambda: svc.remove_backup(song.id), t.TEXT_2, 16),
                   spacing=8, margins=(16, 12, 14, 12))
        lay.setStretchFactor(info, 1)
        self.setLayout(lay)


class BlacklistRow(QFrame):
    """Fila de la lista negra (misma forma que las de cola y respaldo). Los vídeos enseñan su portada."""

    def __init__(self, page: "SongsTab", entry) -> None:
        super().__init__()
        self.setObjectName("row")
        title = ElidedLabel(entry.value)
        title.setStyleSheet("font-size: 14px; font-weight: 600;")
        meta = hbox(Pill(BLACKLIST_KINDS[entry.kind].upper(), t.RED, size=9), spacing=8)
        meta.addStretch(1)
        info = QWidget()
        info.setLayout(vbox(title, meta, spacing=4))
        if entry.kind == "video":
            thumb = Thumbnails.label(entry.value, entry.value)
        else:
            thumb = source_box(entry.kind[:2].upper(), t.TEXT, 44)
        repo = page.svc.repo
        lay = hbox(thumb, 6, info,
                   icon_button("trash-2", "Quitar de la lista negra", lambda: repo.blacklist_remove(entry.id),
                               t.TEXT_2, 16),
                   spacing=8, margins=(16, 12, 14, 12))
        lay.setStretchFactor(info, 1)
        self.setLayout(lay)


class SongsTab(QWidget):
    title = "Canciones & SR"
    icon = "music"

    def __init__(self, service: SongService, bus, player: WebPlayer, owner_name=None, default_platform=None,
                 parent=None, open_overlay=None) -> None:
        super().__init__(parent)
        self.setObjectName("page")
        self.svc = service
        self.owner_name = owner_name or (lambda: "Streamer")
        self.default_platform = default_platform or (lambda: Platform.TWITCH)
        self._status: SongStatus = service.status
        self._pos = 0.0
        self._open_overlay = open_overlay
        self._dur = 0.0

        # ---------------------------------------------------- acciones cabecera
        self._req_btn = button("", object_name="success", slot=self._toggle_requests)
        clear = button("Vaciar Cola", "trash-2", slot=lambda: self.svc.repo.queue_clear_pending())
        self._request_bar = RequestBar()
        self._request_bar.submitted.connect(self._submit_request)
        add = button("Petición / URL", "plus", "primary", self._request_bar.toggle)
        self._req_msg = label("", size=12, wrap=True)
        self._req_msg.hide()
        self.actions = QWidget()
        self._settings_btn = button("Ajustes", "settings", slot=self._open_settings,
                                    tooltip="Reglas de song requests y salida de audio")
        self.actions.setLayout(hbox(self._req_btn, self._settings_btn, spacing=10))

        # ----------------------------------------------------- tarjeta principal
        self._art = QStackedWidget()
        self._art.setFixedSize(ART_SIZE, ART_SIZE)
        self._disc = QLabel()  # la portada de la canción, o el disco si no suena nada
        self._disc.setAlignment(Qt.AlignCenter)
        self._show_cover(None)
        self._art.addWidget(self._disc)
        player.view.setMinimumSize(1, 1)
        self._art.addWidget(player.view)  # oculto: solo se ve con el botón del ojo

        self._state_pill = Pill("DETENIDO", t.TEXT_3, size=10)
        self._by = label("", color=t.TEXT_3, size=12, mono=True)
        self._src_pill = Pill("", t.TEXT_2, size=9)
        self._title = ElidedLabel("Nada sonando")
        self._title.setStyleSheet("font-size: 20px; font-weight: 700;")
        self._channel = label("", color=t.TEXT_2, size=12, mono=True)
        info = QWidget()
        info.setLayout(vbox(hbox(self._state_pill, self._by, self._src_pill, None, spacing=10),
                            self._title, self._channel, spacing=6))
        self._fav_btn = icon_button("star", "", self._toggle_favorite, t.TEXT_2, 22, "iconBox")
        self._fav_btn.hide()
        self._ban_btn = icon_button("ban", "Bloquear este vídeo (lista negra) y saltarlo", self._block_current,
                                    t.RED, 14, "iconBox")
        self._ban_btn.setFixedSize(BAN_SIZE, BAN_SIZE)
        self._ban_btn.hide()
        top = hbox(self._art, info, self._fav_btn, icon_label("audio-lines", t.ACCENT, 32), spacing=20)
        top.setStretchFactor(info, 1)

        self._play = icon_button("play", "Reproducir / pausar", self._toggle_play, "#ffffff", 20, "playBtn")
        self._play.setFixedSize(50, 50)
        self._play.setStyleSheet("border-radius: 25px;")
        self._prev = icon_button("skip-back", "Canción anterior", lambda: self.svc.previous(), t.TEXT_2, 20)
        nxt = icon_button("skip-forward", "Saltar", lambda: self.svc.skip(), t.TEXT_2, 20)
        stop = icon_button("square", "Detener", lambda: self.svc.stop(), t.TEXT_2, 17)
        self._shuffle = icon_button("shuffle", "", lambda: self.svc.set_shuffle(not self._status.shuffle),
                                    t.TEXT_2, 18)
        self._repeat = icon_button("repeat", "", lambda: self.svc.set_repeat(not self._status.repeat),
                                   t.TEXT_2, 18)
        shuffle, prev, repeat = self._shuffle, self._prev, self._repeat
        self._cur = label("00:00", color=t.TEXT_2, size=12, mono=True)
        self._bar = SeekBar()
        self._bar.seeked.connect(self._seek)
        self._bar.scrubbed.connect(lambda f: self._cur.setText(fmt_mmss(f * self._dur)))
        self._total = label("--:--", color=t.TEXT_2, size=12, mono=True)
        self._volume = QSlider(Qt.Horizontal)
        self._volume.setRange(0, 100)
        self._volume.setFixedWidth(100)
        self._volume.setValue(service.config.volume)
        self._vol_lbl = label(f"{service.config.volume}%", color=t.TEXT_2, size=11, mono=True)
        self._volume.valueChanged.connect(lambda v: self._vol_lbl.setText(f"{v}%"))
        self._volume.sliderReleased.connect(lambda: self.svc.set_volume(self._volume.value()))
        obs = button("Overlay OBS", "monitor", "outlineAccent", self._overlay, icon_color=t.ACCENT_SOFT,
                     tooltip="Tarjeta «Sonando ahora» con portada para OBS (Fuente de navegador): "
                             "diseño, URL y activar/desactivar.")
        self._video_btn = icon_button("eye-off", "Mostrar el vídeo (solo para verlo; se controla con estos botones. "
                                      "Si YouTube pide aceptar las cookies, se puede responder ahí)",
                                      self._toggle_video, t.TEXT_2, 17, "iconBox")
        controls = hbox(shuffle, prev, self._play, nxt, repeat, stop, 12, self._cur, self._bar, self._total, 12,
                        icon_label("volume-2", t.TEXT_2, 18), self._volume, self._vol_lbl, 8, obs, self._video_btn,
                        spacing=10)
        controls.setStretchFactor(self._bar, 1)
        now_card = frame("cardHi", vbox(top, frame("hline"), controls, spacing=16, margins=(24, 22, 24, 20)))
        # El botón de bloquear flota en la esquina superior derecha, como el de cerrar de una ventana
        self._ban_btn.setParent(now_card)
        now_card.installEventFilter(self)
        self._now_card = now_card

        # -------------------------------------------- pestañas: cola | respaldo
        self._tabs = ChipGroup([("Cola de reproducción", "queue", "list-music"),
                                ("Playlist de respaldo", "backup", "history"),
                                ("Lista negra", "blacklist", "ban")])
        self._tabs.set_value("queue")
        self._tabs.changed.connect(self._show_list)

        # ------------------------------------------------------------- cola
        self._filter = SearchBox("Filtrar en cola…")
        self._filter.setFixedWidth(220)
        self._filter.textChanged.connect(self.reload)
        self._source = QComboBox()
        self._source.addItem("Todas las fuentes", None)
        self._source.addItem(t.icon("tv", t.ACCENT), "Twitch", Platform.TWITCH)
        self._source.addItem(t.icon("zap", t.KICK), "Kick", Platform.KICK)
        self._source.currentIndexChanged.connect(self.reload)
        queue_head = hbox(add, clear, self._request_bar, None, self._filter, self._source, spacing=12)
        self._queue_list = vbox(spacing=10)
        self._queue_empty = label("La cola está vacía. Los viewers piden canciones con !sr <enlace o nombre>.",
                                  color=t.TEXT_3, size=13, wrap=True)
        queue_page = QWidget()
        queue_page.setLayout(vbox(queue_head, self._req_msg, self._queue_list, self._queue_empty, None, spacing=12))
        # Las dos páginas van en el mismo layout y solo se ve una (un QStackedWidget no daba
        # a la lista la altura que necesita dentro del área con scroll)
        self._queue_page, self._backup_page = queue_page, self._build_backup()
        self._blacklist_page = self._build_blacklist()
        self._backup_page.hide()
        self._blacklist_page.hide()
        queue_col = QWidget()
        queue_col.setLayout(vbox(self._tabs, self._queue_page, self._backup_page, self._blacklist_page, spacing=14))

        # ------------------------------------------------------ columna derecha
        # Reglas y salida de audio son configuración: van en una ventana modal («Ajustes»)
        self._settings_dlg = SongSettingsDialog(self, self._build_rules(), self._build_output_help())

        lower = hbox(queue_col, spacing=22)
        lower.setStretchFactor(queue_col, 1)
        inner = QWidget()
        inner.setLayout(vbox(now_card, 4, lower, spacing=18, margins=(28, 22, 28, 24)))
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        area.setWidget(inner)
        self.setLayout(vbox(area, spacing=0))

        bus.songs_changed.connect(self.reload)
        bus.song_status.connect(self._on_status)
        self.reload()
        self._on_status(service.status)

    # ------------------------------------------------------------- tarjetas
    def _build_rules(self) -> QFrame:
        cfg = self.svc.config
        self._saved = label("", color=t.GREEN, size=11, mono=True)
        self._perm = QComboBox()
        for p in Permission:
            self._perm.addItem("Todos los viewers" if p is Permission.EVERYONE else p.label, int(p))
        self._perm.setCurrentIndex(cfg.permission)
        self._perm.currentIndexChanged.connect(self._save_rules)

        self._max_user = QSlider(Qt.Horizontal)
        self._max_user.setRange(0, 10)
        self._max_user.setValue(min(cfg.max_per_user, 10))
        self._max_user_lbl = label("", color=t.TEXT, size=12, mono=True, bold=True)
        self._max_dur = QSlider(Qt.Horizontal)
        self._max_dur.setRange(0, 30)
        self._max_dur.setValue(min(cfg.max_duration_min, 30))
        self._max_dur_lbl = label("", color=t.TEXT, size=12, mono=True, bold=True)
        for s in (self._max_user, self._max_dur):
            s.valueChanged.connect(self._update_rule_labels)
            s.sliderReleased.connect(self._save_rules)
            s.valueChanged.connect(lambda _v, sl=s: None if sl.isSliderDown() else self._save_rules())
        self._update_rule_labels()
        body = vbox(
            hbox(icon_label("settings", t.ACCENT_SOFT, 16), label("REGLAS DE SONG REQUESTS", "sectionTitle"), None,
                 self._saved, spacing=8),
            8,
            label("Permitir peticiones a:", color=t.TEXT_2, size=12, mono=True), self._perm,
            6,
            hbox(label("Máx. canciones / usuario", color=t.TEXT_2, size=12, mono=True), None, self._max_user_lbl),
            self._max_user,
            hbox(label("Duración máx. por vídeo", color=t.TEXT_2, size=12, mono=True), None, self._max_dur_lbl),
            self._max_dur,
            hbox(label("Requerir Puntos de Canal (Twitch)", color=t.TEXT_3, size=12, mono=True), None,
                 soon(Toggle(False), "puntos de canal")),
            label("El streamer no tiene límite de duración ni de cola.", color=t.TEXT_3, size=11, wrap=True),
            spacing=8, margins=(20, 18, 20, 18),
        )
        return frame("card", body)

    def _build_output_help(self) -> QFrame:
        """La salida del reproductor no se elige aquí: YouTube necesitaría permiso de micrófono."""
        return frame("card", vbox(
            hbox(icon_label("volume-2", t.ACCENT_SOFT, 16), label("SALIDA DE AUDIO", "sectionTitle"), None, spacing=8),
            label("Para enviar la música a otro canal o dispositivo, pon una canción y asigna "
                  "<b>QtWebEngineProcess</b> en Wave Link o en Configuración de Windows → Sistema → Sonido → "
                  "Mezclador de volumen.", color=t.TEXT_2, size=12, wrap=True),
            spacing=10, margins=(20, 18, 20, 18),
        ))

    def _show_list(self, key: str) -> None:
        self._queue_page.setVisible(key == "queue")
        self._backup_page.setVisible(key == "backup")
        self._blacklist_page.setVisible(key == "blacklist")

    def _build_backup(self) -> QWidget:
        self._backup_input = QLineEdit()
        self._backup_input.setPlaceholderText("Añadir: enlace de YouTube o nombre de la canción")
        self._backup_input.returnPressed.connect(self._add_backup)
        self._backup_filter = SearchBox("Filtrar en respaldo…")
        self._backup_filter.setFixedWidth(220)
        self._backup_filter.textChanged.connect(self._reload_backup)
        self._backup_msg = label("", size=11, wrap=True)
        self._backup_list = vbox(spacing=10)
        self._backup_empty = label("", color=t.TEXT_3, size=13, wrap=True)
        page = QWidget()
        page.setLayout(vbox(
            hbox(self._backup_input, icon_button("plus", "Añadir al respaldo", self._add_backup, t.TEXT, 16, "iconBox"),
                 12, self._backup_filter, spacing=8),
            label("Suena en bucle cuando no hay peticiones (solo si el reproductor está en marcha), en orden "
                  "o al azar con el botón de aleatorio. La ★ de una petición también la añade aquí.",
                  color=t.TEXT_3, size=12, wrap=True),
            self._backup_msg, self._backup_list, self._backup_empty, None,
            spacing=12,
        ))
        return page

    def _build_blacklist(self) -> QWidget:
        self._bl_kind = QComboBox()
        for k, text in BLACKLIST_KINDS.items():
            self._bl_kind.addItem(text, k)
        self._bl_value = QLineEdit()
        self._bl_value.setPlaceholderText("Enlace de vídeo, canal o palabra en el título")
        self._bl_value.returnPressed.connect(self._add_blacklist)
        self._bl_msg = label("", color=t.RED, size=11, wrap=True)
        self._bl_list = vbox(spacing=10)
        self._bl_empty = label("La lista negra está vacía. Aquí aparece lo que bloquees; se quita con la papelera.",
                               color=t.TEXT_3, size=13, wrap=True)
        page = QWidget()
        page.setLayout(vbox(
            hbox(self._bl_kind, self._bl_value,
                 icon_button("plus", "Bloquear", self._add_blacklist, t.TEXT, 16, "iconBox"), spacing=8),
            label("Los vídeos, canales o palabras de esta lista no se aceptan como petición. El botón de la tarjeta "
                  "de reproducción bloquea y salta lo que suena.", color=t.TEXT_3, size=12, wrap=True),
            self._bl_msg, self._bl_list, self._bl_empty, None,
            spacing=12,
        ))
        return page

    # ------------------------------------------------------------- acciones
    def _open_settings(self) -> None:
        self._settings_dlg.open()

    def _toggle_requests(self) -> None:
        self.svc.set_enabled(not self.svc.config.enabled)

    def _toggle_play(self) -> None:
        st = self._status
        if st.current is not None and not st.paused:
            self.svc.pause()
        else:
            self.svc.play()

    def _toggle_favorite(self) -> None:
        if self._status.current is not None:
            self.svc.toggle_favorite(self._status.current)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt)
        if obj is getattr(self, "_now_card", None) and event.type() in (QEvent.Resize, QEvent.Show):
            self._place_ban_button()
        return super().eventFilter(obj, event)

    def _place_ban_button(self) -> None:
        card = self._now_card
        self._ban_btn.move(card.width() - BAN_SIZE - BAN_MARGIN, BAN_MARGIN)
        self._ban_btn.raise_()

    def _block_current(self) -> None:
        if self._status.current is not None:
            self.svc.block_song(self._status.current)

    def _update_favorite(self) -> None:
        cur = self._status.current
        self._fav_btn.setVisible(cur is not None)
        self._ban_btn.setVisible(cur is not None)
        if cur is not None:
            set_star(self._fav_btn, self.svc.is_favorite(cur.video_id), 22)

    def _seek(self, fraction: float) -> None:
        if self._dur:
            pos = fraction * self._dur
            self.svc.seek(pos)
            self.set_progress(pos, self._dur)

    def _show_cover(self, song: Song | None) -> None:
        """Portada de YouTube en el cuadro del disco (mientras llega, o si falla, el disco)."""
        self._disc.setProperty("thumb_id", "")  # descarta una portada anterior que aún no llegó
        self._disc.setPixmap(t.pixmap("disc-3", t.ACCENT, 56))
        self._disc.setStyleSheet(f"background: {t.BG_900}; border: 1px solid {t.BORDER}; border-radius: 14px;")
        if song is not None:
            Thumbnails.load(self._disc, song.video_id, ART_SIZE, radius=14)

    def _toggle_video(self) -> None:
        showing = self._art.currentIndex() == 1
        self._art.setCurrentIndex(0 if showing else 1)
        self._art.setFixedSize(*((ART_SIZE, ART_SIZE) if showing else (320, 180)))
        self._video_btn.setIcon(t.icon("eye-off" if showing else "monitor", t.TEXT_2, 17))

    def _overlay(self) -> None:
        if self._open_overlay is not None:
            self._open_overlay()

    def _update_rule_labels(self) -> None:
        mu, md = self._max_user.value(), self._max_dur.value()
        self._max_user_lbl.setText(str(mu) if mu else "Sin límite")
        self._max_dur_lbl.setText(f"{md:02d}:00 min" if md else "Sin límite")

    def _save_rules(self) -> None:
        self.svc.update_settings(Permission(self._perm.currentData()), self._max_dur.value(), self._max_user.value())
        self._saved.setText("Guardado")

    def _add_backup(self) -> None:
        text = self._backup_input.text().strip()
        if text:
            asyncio.ensure_future(self._do_add_backup(text))

    async def _do_add_backup(self, text: str) -> None:
        self._backup_msg.setText(f"<span style='color:{t.TEXT_3}'>Buscando…</span>")
        try:
            info = await self.svc.find_video(text)
            self.svc.repo.backup_add(info)
        except (YouTubeError, ValueError) as exc:
            self._backup_msg.setText(f"<span style='color:{t.RED}'>{exc}</span>")
            return
        self._backup_input.clear()
        self._backup_msg.setText(f"<span style='color:{t.GREEN}'>Añadida: {info.title}</span>")

    def _add_blacklist(self) -> None:
        try:
            self.svc.repo.blacklist_add(self._bl_kind.currentData(), self._bl_value.text())
        except ValueError as exc:
            self._bl_msg.setText(str(exc))
            return
        self._bl_value.clear()
        self._bl_msg.clear()

    def _submit_request(self, text: str) -> None:
        asyncio.ensure_future(self._do_request(text))

    async def _do_request(self, text: str) -> None:
        bar = self._request_bar
        bar.set_busy(True)
        self._req_msg.setText(f"<span style='color:{t.TEXT_3}'>Buscando…</span>")
        self._req_msg.show()
        try:
            await self.svc.add_from_app(text, self.owner_name(), self.default_platform())
        except (YouTubeError, ValueError) as exc:
            self._req_msg.setText(f"<span style='color:{t.RED}'>{exc}</span>")
            bar.set_busy(False)
            return
        bar.set_busy(False)
        bar.clear()
        bar.collapse()
        self._req_msg.hide()

    # ------------------------------------------------------------- refresco
    def reload(self) -> None:
        repo = self.svc.repo
        pending = repo.queue_pending()
        backup = repo.backup_list()
        self._tabs.set_text("queue", f"Cola de reproducción ({len(pending)})")
        self._tabs.set_text("backup", f"Playlist de respaldo ({len(backup)})")
        query = self._filter.text().strip().lower()
        source = self._source.currentData()
        cur = self._status.current
        eta = (max(0, int(self._dur - self._pos)) if self._dur else cur.duration_s) if cur else 0
        clear_layout(self._queue_list)
        favorites = {s.video_id for s in backup}
        shown = 0
        for i, s in enumerate(pending, 1):
            if (not query or query in s.title.lower() or query in s.requested_by.lower()) and \
                    (source is None or s.platform is source):
                self._queue_list.addWidget(QueueRow(self, s, i, eta, s.video_id in favorites))
                shown += 1
            eta += s.duration_s
        self._queue_empty.setVisible(shown == 0)
        self._reload_backup(backup)

        blocked = repo.blacklist_list()
        self._tabs.set_text("blacklist", f"Lista negra ({len(blocked)})")
        clear_layout(self._bl_list)
        for e in blocked:
            self._bl_list.addWidget(BlacklistRow(self, e))
        self._bl_empty.setVisible(not blocked)
        self._update_favorite()

    def _reload_backup(self, backup: list[Song] | None = None) -> None:
        if not isinstance(backup, list):  # también llega el texto del buscador (señal textChanged)
            backup = self.svc.repo.backup_list()
        query = self._backup_filter.text().strip().lower()
        st = self._status
        playing_id = st.current.id if st.current is not None and st.from_backup else None
        clear_layout(self._backup_list)
        shown = 0
        for i, s in enumerate(backup, 1):
            if not query or query in s.title.lower() or query in s.channel.lower():
                self._backup_list.addWidget(BackupRow(self, s, i, s.id == playing_id))
                shown += 1
        self._backup_empty.setText("No hay coincidencias." if backup else
                                   "Vacía. Añade canciones arriba o con la ★ de una petición.")
        self._backup_empty.setVisible(shown == 0)

    def _on_status(self, st: SongStatus) -> None:
        old = self._status.current
        self._status = st
        if old != st.current:
            self._reload_backup()  # resalta la que suena
        if (old and old.video_id) != (st.current and st.current.video_id):
            self._show_cover(st.current)
        self._update_favorite()
        s = st.current
        enabled = self.svc.config.enabled
        self._req_btn.setText(f"Peticiones: {'ACTIVO' if enabled else 'CERRADO'}")
        self._req_btn.setObjectName("success" if enabled else "danger")
        self._req_btn.setIcon(t.icon("radio", t.GREEN if enabled else t.RED, 12))
        self._req_btn.style().unpolish(self._req_btn)
        self._req_btn.style().polish(self._req_btn)
        if s is None:
            self._state_pill.set("DETENIDO", t.TEXT_3)
            self._set_title("Nada sonando")
            self._by.setText("Pulsa reproducir o espera a la primera petición")
            self._channel.setText("")
            self._src_pill.hide()
            self.set_progress(0, 0)
        else:
            if st.paused:
                self._state_pill.set("EN PAUSA", t.AMBER, "pause")
            else:
                self._state_pill.set("EN REPRODUCCIÓN", t.GREEN, "radio")
            self._set_title(s.title)
            if st.from_backup or not s.requested_by:
                self._by.setText("Lista de respaldo")
                self._src_pill.hide()
            else:
                self._by.setText(f"Solicitado por: <b style='color:{t.TEXT}'>{s.requested_by}</b>")
                if s.platform in _SOURCE:
                    self._src_pill.set(*_SOURCE[s.platform])
                    self._src_pill.show()
            self._channel.setText(f"{s.channel} · {format_duration(s.duration_s)}" if s.channel
                                  else format_duration(s.duration_s))
        self._play.setIcon(t.icon("pause" if s is not None and not st.paused else "play", "#ffffff", 20))
        self._prev.setEnabled(st.has_previous)
        self._shuffle.setIcon(t.icon("shuffle", t.ACCENT if st.shuffle else t.TEXT_2, 18))
        self._shuffle.setToolTip("Aleatorio en el respaldo: " + ("activado" if st.shuffle else "desactivado"))
        self._repeat.setIcon(t.icon("repeat", t.ACCENT if st.repeat else t.TEXT_2, 18))
        self._repeat.setToolTip("Repetir la canción actual: " + ("activado" if st.repeat else "desactivado"))
        if not self._volume.isSliderDown():
            self._volume.setValue(st.volume)

    def _set_title(self, text: str) -> None:
        self._title._full = text  # noqa: SLF001 (ElidedLabel)
        self._title.setToolTip(text)
        self._title.setText(self._title.fontMetrics().elidedText(text, Qt.ElideRight, max(40, self._title.width())))

    def set_progress(self, pos: float, dur: float) -> None:
        self._pos, self._dur = pos, dur
        if not self._bar.dragging:
            self._cur.setText(fmt_mmss(pos))
        self._total.setText(fmt_mmss(dur) if dur else "--:--")
        self._bar.set_seekable(dur > 0)
        self._bar.set_value(pos / dur if dur else 0)


class RequestBar(QWidget):
    """«Petición / URL»: un campo que se despliega de izquierda a derecha junto a los botones de la cola."""

    submitted = Signal(str)
    WIDTH = 340

    def __init__(self) -> None:
        super().__init__()
        self._open = False
        self._inner = QWidget(self)  # ancho fijo: al crecer la barra se descubre de izquierda a derecha
        self._input = QLineEdit()
        self._input.setPlaceholderText("Enlace de YouTube o «artista - canción»")
        self._input.setMinimumHeight(36)
        self._input.returnPressed.connect(self._submit)
        self._ok = button("Añadir", "plus", "primary", self._submit)
        close = icon_button("x", "Cerrar (Esc)", self.collapse, t.TEXT_2, 16)
        self._inner.setLayout(hbox(self._input, self._ok, close, spacing=8))
        self._inner.setFixedWidth(self.WIDTH)
        self.setFixedHeight(max(36, self._inner.sizeHint().height()))
        self.setFixedWidth(0)
        self.hide()
        # Ancho fijo en cada paso (no solo el máximo): así el layout le reserva sitio de verdad
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(260)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(lambda v: self.setFixedWidth(int(v)))
        self._anim.finished.connect(self._on_finished)

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt)
        self._inner.setGeometry(0, 0, self.WIDTH, self.height())
        super().resizeEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt)
        if event.key() == Qt.Key_Escape:
            self.collapse()
        else:
            super().keyPressEvent(event)

    def toggle(self) -> None:
        self.collapse() if self._open else self.expand()

    def expand(self) -> None:
        self._open = True
        self.show()
        self._slide(self.WIDTH)
        self._input.setFocus()

    def collapse(self) -> None:
        self._open = False
        self._slide(0)

    def _slide(self, end: int) -> None:
        self._anim.stop()
        self._anim.setStartValue(self.width())
        self._anim.setEndValue(end)
        self._anim.start()

    def _on_finished(self) -> None:
        if not self._open:
            self.hide()

    def _submit(self) -> None:
        text = self._input.text().strip()
        if text and self._ok.isEnabled():
            self.submitted.emit(text)

    def set_busy(self, busy: bool) -> None:
        self._ok.setEnabled(not busy)

    def clear(self) -> None:
        self._input.clear()


class SongSettingsDialog(ModalDialog):
    """«Ajustes»: reglas de song requests y ayuda de la salida de audio. Se crea una vez y se reabre."""

    def __init__(self, parent: QWidget, rules: QWidget, output_help: QWidget) -> None:
        super().__init__("Ajustes de Song Requests", "Quién puede pedir canciones, límites y salida de audio. "
                         "Los cambios se guardan al momento.", "settings", parent, width=560)
        self.body.addWidget(rules)
        self.body.addWidget(output_help)
        self.footer.addStretch(1)
        self.footer.addWidget(button("Cerrar", slot=self.accept))
