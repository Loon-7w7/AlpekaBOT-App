"""Página «Canciones & Song Requests»: reproductor, cola, reglas, respaldo y lista negra."""

from __future__ import annotations

import asyncio
import logging

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter
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
from ..paths import NOW_PLAYING_FILE
from . import theme as t
from .commands_tab import ElidedLabel
from .widgets import (
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
        lay = hbox(label(f"#{index}", size=14, bold=True, mono=True, color=t.ACCENT_SOFT), 4, source_box(), 6, info,
                   self.fav_btn,
                   soon(icon_button("play", "", None, t.ACCENT_SOFT, 16, "iconBox"), "reproducir ya"),
                   icon_button("chevron-up", "Subir", lambda: repo.queue_move(song.id, -1), t.TEXT_2, 16),
                   icon_button("chevron-down", "Bajar", lambda: repo.queue_move(song.id, 1), t.TEXT_2, 16),
                   icon_button("trash-2", "Quitar de la cola", lambda: page.svc.remove(song.id), t.TEXT_2, 16),
                   spacing=8, margins=(16, 12, 14, 12))
        lay.setStretchFactor(info, 1)
        self.setLayout(lay)


class MiniRow(QFrame):
    """Fila compacta para la lista de respaldo y la lista negra."""

    def __init__(self, badge: str, title: str, subtitle: str, actions: list[QWidget]) -> None:
        super().__init__()
        self.setObjectName("cardInner")
        t_lbl = ElidedLabel(title)
        t_lbl.setStyleSheet("font-weight: 600;")
        texts = QWidget()
        texts.setLayout(vbox(t_lbl, label(subtitle, color=t.TEXT_3, size=11, mono=True), spacing=2))
        lay = hbox(source_box(badge, t.TEXT, 36), texts, *actions, spacing=8, margins=(10, 8, 8, 8))
        lay.setStretchFactor(texts, 1)
        self.setLayout(lay)


class SongsTab(QWidget):
    title = "Canciones & SR"
    icon = "music"

    def __init__(self, service: SongService, bus, player: WebPlayer, owner_name=None, default_platform=None,
                 parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("page")
        self.svc = service
        self.owner_name = owner_name or (lambda: "Streamer")
        self.default_platform = default_platform or (lambda: Platform.TWITCH)
        self._status: SongStatus = service.status
        self._pos = 0.0
        self._dur = 0.0

        # ---------------------------------------------------- acciones cabecera
        self._req_btn = button("", object_name="success", slot=self._toggle_requests)
        clear = button("Vaciar Cola", "trash-2", slot=lambda: self.svc.repo.queue_clear_pending())
        add = button("Petición / URL", "plus", "primary", self._open_request)
        self.actions = QWidget()
        self.actions.setLayout(hbox(self._req_btn, clear, add, spacing=10))

        # ----------------------------------------------------- tarjeta principal
        self._art = QStackedWidget()
        self._art.setFixedSize(110, 110)
        disc = QLabel()
        disc.setAlignment(Qt.AlignCenter)
        disc.setPixmap(t.pixmap("disc-3", t.ACCENT, 56))
        disc.setStyleSheet(f"background: {t.BG_900}; border: 1px solid {t.BORDER}; border-radius: 14px;")
        self._art.addWidget(disc)
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
        top = hbox(self._art, info, self._fav_btn, icon_label("audio-lines", t.ACCENT, 32), spacing=20)
        top.setStretchFactor(info, 1)

        self._play = icon_button("play", "Reproducir / pausar", self._toggle_play, "#ffffff", 20, "playBtn")
        self._play.setFixedSize(50, 50)
        self._play.setStyleSheet("border-radius: 25px;")
        prev = soon(icon_button("skip-back", "", None, t.TEXT_2, 20), "canción anterior")
        nxt = icon_button("skip-forward", "Saltar", lambda: self.svc.skip(), t.TEXT_2, 20)
        stop = icon_button("square", "Detener", lambda: self.svc.stop(), t.TEXT_2, 17)
        shuffle = soon(icon_button("shuffle", "", None, t.TEXT_2, 18), "aleatorio")
        repeat = soon(icon_button("repeat", "", None, t.TEXT_2, 18), "repetir")
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
        obs = button("Archivo OBS", "monitor", "outlineAccent", self._copy_obs, icon_color=t.ACCENT_SOFT,
                     tooltip=f"Copia la ruta de {NOW_PLAYING_FILE.name}: en OBS, añade «Texto (GDI+)» y "
                             "marca «Leer desde archivo».")
        self._video_btn = icon_button("eye-off", "Mostrar el vídeo (solo para verlo; se controla con estos botones. "
                                      "Si YouTube pide aceptar las cookies, se puede responder ahí)",
                                      self._toggle_video, t.TEXT_2, 17, "iconBox")
        controls = hbox(shuffle, prev, self._play, nxt, repeat, stop, 12, self._cur, self._bar, self._total, 12,
                        icon_label("volume-2", t.TEXT_2, 18), self._volume, self._vol_lbl, 8, obs, self._video_btn,
                        spacing=10)
        controls.setStretchFactor(self._bar, 1)
        now_card = frame("cardHi", vbox(top, frame("hline"), controls, spacing=16, margins=(24, 22, 24, 20)))

        # ------------------------------------------------------------- cola
        self._pending_pill = Pill("0 pendientes", t.ACCENT_SOFT, size=11)
        self._filter = SearchBox("Filtrar en cola…")
        self._filter.setFixedWidth(220)
        self._filter.textChanged.connect(self.reload)
        self._source = QComboBox()
        self._source.addItem("Todas las fuentes", None)
        self._source.addItem(t.icon("tv", t.ACCENT), "Twitch", Platform.TWITCH)
        self._source.addItem(t.icon("zap", t.KICK), "Kick", Platform.KICK)
        self._source.currentIndexChanged.connect(self.reload)
        queue_head = hbox(label("COLA DE REPRODUCCIÓN", "sectionTitle"), self._pending_pill, None, self._filter,
                          self._source, spacing=12)
        self._queue_list = vbox(spacing=10)
        self._queue_empty = label("La cola está vacía. Los viewers piden canciones con !sr <enlace o nombre>.",
                                  color=t.TEXT_3, size=13, wrap=True)
        queue_col = QWidget()
        queue_col.setLayout(vbox(queue_head, self._queue_list, self._queue_empty, None, spacing=12))

        # ------------------------------------------------------ columna derecha
        side = QWidget()
        side.setFixedWidth(400)
        side.setLayout(vbox(self._build_rules(), self._build_output_help(), self._build_backup(),
                            self._build_blacklist(), None, spacing=16))

        lower = hbox(queue_col, side, spacing=22)
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

    def _build_backup(self) -> QFrame:
        self._backup_input = QLineEdit()
        self._backup_input.setPlaceholderText("Enlace de YouTube o nombre")
        self._backup_input.returnPressed.connect(self._add_backup)
        self._backup_msg = label("", size=11, wrap=True)
        self._backup_list = vbox(spacing=8)
        body = vbox(
            hbox(icon_label("history", t.TEXT_2, 16), label("PLAYLIST DE RESPALDO", "sectionTitle"), None, spacing=8),
            label("Suena en orden y en bucle cuando no hay peticiones (solo si el reproductor está en marcha).",
                  color=t.TEXT_2, size=12, wrap=True),
            hbox(self._backup_input, icon_button("plus", "Añadir al respaldo", self._add_backup, t.TEXT, 16, "iconBox")),
            self._backup_msg, self._backup_list,
            spacing=10, margins=(20, 18, 20, 18),
        )
        return frame("card", body)

    def _build_blacklist(self) -> QFrame:
        self._bl_kind = QComboBox()
        for k, text in BLACKLIST_KINDS.items():
            self._bl_kind.addItem(text, k)
        self._bl_value = QLineEdit()
        self._bl_value.setPlaceholderText("Vídeo, canal o palabra")
        self._bl_value.returnPressed.connect(self._add_blacklist)
        self._bl_msg = label("", color=t.RED, size=11, wrap=True)
        self._bl_list = vbox(spacing=8)
        body = vbox(
            hbox(icon_label("ban", t.RED, 16), label("LISTA NEGRA", "sectionTitle"), None, spacing=8),
            hbox(self._bl_kind, self._bl_value,
                 icon_button("plus", "Bloquear", self._add_blacklist, t.TEXT, 16, "iconBox")),
            self._bl_msg, self._bl_list,
            spacing=10, margins=(20, 18, 20, 18),
        )
        return frame("card", body)

    # ------------------------------------------------------------- acciones
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

    def _update_favorite(self) -> None:
        cur = self._status.current
        self._fav_btn.setVisible(cur is not None)
        if cur is not None:
            set_star(self._fav_btn, self.svc.is_favorite(cur.video_id), 22)

    def _seek(self, fraction: float) -> None:
        if self._dur:
            pos = fraction * self._dur
            self.svc.seek(pos)
            self.set_progress(pos, self._dur)

    def _toggle_video(self) -> None:
        showing = self._art.currentIndex() == 1
        self._art.setCurrentIndex(0 if showing else 1)
        self._art.setFixedSize(*((110, 110) if showing else (320, 180)))
        self._video_btn.setIcon(t.icon("eye-off" if showing else "monitor", t.TEXT_2, 17))

    def _copy_obs(self) -> None:
        QGuiApplication.clipboard().setText(str(NOW_PLAYING_FILE))
        self._saved.setText("Ruta copiada")

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

    def _open_request(self) -> None:
        dlg = RequestDialog(self, self)
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        dlg.open()

    # ------------------------------------------------------------- refresco
    def reload(self) -> None:
        repo = self.svc.repo
        pending = repo.queue_pending()
        self._pending_pill.set(f"{len(pending)} pendientes", t.ACCENT_SOFT)
        query = self._filter.text().strip().lower()
        source = self._source.currentData()
        cur = self._status.current
        eta = (max(0, int(self._dur - self._pos)) if self._dur else cur.duration_s) if cur else 0
        clear_layout(self._queue_list)
        favorites = {s.video_id for s in repo.backup_list()}
        shown = 0
        for i, s in enumerate(pending, 1):
            if (not query or query in s.title.lower() or query in s.requested_by.lower()) and \
                    (source is None or s.platform is source):
                self._queue_list.addWidget(QueueRow(self, s, i, eta, s.video_id in favorites))
                shown += 1
            eta += s.duration_s
        self._queue_empty.setVisible(shown == 0)

        clear_layout(self._backup_list)
        backup = repo.backup_list()
        for i, s in enumerate(backup, 1):
            self._backup_list.addWidget(MiniRow(initials(s.title), s.title,
                                                f"Respaldo #{i} · {format_duration(s.duration_s)}", [
                icon_button("chevron-up", "Subir", lambda _=False, x=s: repo.backup_move(x.id, -1), t.TEXT_3, 14),
                icon_button("chevron-down", "Bajar", lambda _=False, x=s: repo.backup_move(x.id, 1), t.TEXT_3, 14),
                icon_button("trash-2", "Quitar", lambda _=False, x=s: self.svc.remove_backup(x.id), t.TEXT_3, 14),
            ]))
        if not backup:
            self._backup_list.addWidget(label("Vacía.", color=t.TEXT_3, size=12))

        clear_layout(self._bl_list)
        for e in repo.blacklist_list():
            self._bl_list.addWidget(MiniRow(e.kind[:2].upper(), e.value, BLACKLIST_KINDS[e.kind], [
                icon_button("trash-2", "Quitar", lambda _=False, x=e: repo.blacklist_remove(x.id), t.TEXT_3, 14),
            ]))
        self._update_favorite()

    def _on_status(self, st: SongStatus) -> None:
        self._status = st
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


class RequestDialog(ModalDialog):
    """«+ Petición / URL»: añadir una canción a la cola como el streamer."""

    def __init__(self, page: SongsTab, parent=None) -> None:
        super().__init__("Nueva Petición", "Pega un enlace de YouTube o escribe el nombre de la canción. "
                         "Se añade como el streamer (sin límites).", "music", parent, width=620)
        self._page = page
        self._input = QLineEdit()
        self._input.setPlaceholderText("https://youtu.be/…  o  «artista - canción»")
        self._input.setMinimumHeight(42)
        self._input.returnPressed.connect(self._add)
        self._msg = label("", size=12, wrap=True)
        self.body.addWidget(self._input)
        self.body.addWidget(self._msg)
        self.footer.addWidget(button("Cancelar", slot=self.reject))
        self.footer.addStretch(1)
        self._ok = button("Añadir a la cola", "plus", "primary", self._add)
        self.footer.addWidget(self._ok)

    def _add(self) -> None:
        text = self._input.text().strip()
        if text and self._ok.isEnabled():
            self._ok.setEnabled(False)
            asyncio.ensure_future(self._do_add(text))

    async def _do_add(self, text: str) -> None:
        self._msg.setText(f"<span style='color:{t.TEXT_3}'>Buscando…</span>")
        try:
            await self._page.svc.add_from_app(text, self._page.owner_name(), self._page.default_platform())
        except (YouTubeError, ValueError) as exc:
            self._msg.setText(f"<span style='color:{t.RED}'>{exc}</span>")
            self._ok.setEnabled(True)
            return
        self.accept()
