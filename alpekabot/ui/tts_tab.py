"""Página «Texto a voz»: comando, permisos y límites, voz, salida de audio y cola."""

from __future__ import annotations

import asyncio
import logging

from PySide6.QtCore import Qt
from PySide6.QtMultimedia import QMediaDevices
from PySide6.QtWidgets import QComboBox, QFrame, QLineEdit, QSlider, QSpinBox, QWidget

from ..core.models import Permission
from ..features.tts import (
    MAX_CHARS,
    MAX_COOLDOWN_S,
    MAX_QUEUE,
    MAX_RATE,
    MIN_CHARS,
    MIN_QUEUE,
    MIN_RATE,
    SKIP_COMMAND,
    TtsItem,
    TtsService,
)
from . import theme as t
from .commands_tab import ElidedLabel, scroll_page
from .tts_player import output_devices
from .widgets import Pill, Toggle, button, clear_layout, frame, hbox, icon_button, icon_label, label, vbox

log = logging.getLogger(__name__)

DEFAULT_TEST = "Hola, esta es una prueba del texto a voz."


def _plain(text: str, css: str = "") -> ElidedLabel:
    """El texto de los viewers nunca se interpreta como HTML."""
    lbl = ElidedLabel(text)
    lbl.setTextFormat(Qt.PlainText)
    if css:
        lbl.setStyleSheet(css)
    return lbl


def _field(text: str):
    return label(text, color=t.TEXT_2, size=12, mono=True)


def _spin(low: int, high: int, value: int, suffix: str = "") -> QSpinBox:
    s = QSpinBox()
    s.setRange(low, high)
    s.setValue(value)
    s.setSuffix(suffix)
    return s


class QueueRow(QFrame):
    def __init__(self, page: "TtsTab", item: TtsItem) -> None:
        super().__init__()
        self.setObjectName("cardInner")
        text = _plain(item.text, "font-size: 13px;")
        who = item.user_name or "Prueba"
        lay = hbox(_plain(who, f"color: {t.ACCENT_SOFT}; font-weight: 600;"), text,
                   icon_button("trash-2", "Quitar de la cola", lambda: page.svc.remove(item.id), t.TEXT_3, 14),
                   spacing=10, margins=(12, 8, 8, 8))
        lay.setStretchFactor(text, 1)
        self.setLayout(lay)


class TtsTab(QWidget):
    title = "Texto a voz (TTS)"
    icon = "audio-lines"

    def __init__(self, service: TtsService, bus, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("page")
        self.svc = service
        self._voices_loaded = False
        cfg = service.config

        self._enabled = Toggle(cfg.enabled)
        self._enabled.toggled.connect(lambda on: self.svc.update(enabled=on))
        self.actions = QWidget()
        self.actions.setLayout(hbox(label("Activado", color=t.TEXT_2, size=13), self._enabled, spacing=10))

        left = vbox(self._build_status(), self._build_command(), None, spacing=16)
        right = QWidget()
        right.setFixedWidth(460)
        right.setLayout(vbox(self._build_voice(), self._build_output(), None, spacing=16))
        left_w = QWidget()
        left_w.setLayout(left)
        columns = hbox(left_w, right, spacing=22)
        columns.setStretchFactor(left_w, 1)
        self.setLayout(vbox(scroll_page(vbox(columns, None, margins=(28, 22, 28, 24))), spacing=0))

        self._devices = QMediaDevices(self)
        self._devices.audioOutputsChanged.connect(self._fill_devices)
        self._fill_devices()
        bus.tts_changed.connect(self.reload)
        self.reload()

    # ------------------------------------------------------------- tarjetas
    def _build_status(self) -> QFrame:
        self._pill = Pill("", t.TEXT_3, size=10)
        self._now = _plain("", "font-size: 16px; font-weight: 600;")
        self._error = label("", color=t.RED, size=12, wrap=True)
        self._error.setTextFormat(Qt.PlainText)
        self._queue_title = label("", "sectionTitle")
        self._queue_list = vbox(spacing=8)
        head = hbox(icon_label("audio-lines", t.ACCENT, 18), self._pill, self._now,
                    button("Saltar", "skip-forward", slot=lambda: self.svc.skip()),
                    button("Vaciar cola", "trash-2", slot=lambda: self.svc.clear()), spacing=10)
        head.setStretchFactor(self._now, 1)
        return frame("cardHi", vbox(head, self._error, frame("hline"), self._queue_title, self._queue_list,
                                    spacing=12, margins=(22, 18, 22, 18)))

    def _build_command(self) -> QFrame:
        cfg = self.svc.config
        self._command = QLineEdit(cfg.command)
        self._command.setMaxLength(33)
        self._command.editingFinished.connect(self._save_command)
        self._command_msg = label("", size=11, wrap=True)
        self._perm = QComboBox()
        for p in Permission:
            self._perm.addItem("Todos los viewers" if p is Permission.EVERYONE else p.label, int(p))
        self._perm.setCurrentIndex(cfg.permission)
        self._perm.currentIndexChanged.connect(lambda _i: self.svc.update(permission=self._perm.currentData()))
        self._cooldown = _spin(0, MAX_COOLDOWN_S, cfg.cooldown_s, " s")
        self._max_chars = _spin(MIN_CHARS, MAX_CHARS, cfg.max_chars, " caracteres")
        self._max_queue = _spin(MIN_QUEUE, MAX_QUEUE, cfg.max_queue, " mensajes")
        self._cooldown.valueChanged.connect(lambda v: self.svc.update(cooldown_s=v))
        self._max_chars.valueChanged.connect(lambda v: self.svc.update(max_chars=v))
        self._max_queue.valueChanged.connect(lambda v: self.svc.update(max_queue=v))
        self._read_name = Toggle(cfg.read_name)
        self._read_name.toggled.connect(lambda on: self.svc.update(read_name=on))
        return frame("card", vbox(
            hbox(icon_label("terminal", t.ACCENT_SOFT, 16), label("COMANDO Y LÍMITES", "sectionTitle"), None, spacing=8),
            _field("Comando en Twitch y Kick:"), hbox(label("!", size=16, bold=True, mono=True), self._command, spacing=6),
            self._command_msg,
            _field("Pueden usarlo:"), self._perm,
            hbox(vbox(_field("Cooldown global:"), self._cooldown, spacing=6),
                 vbox(_field("Longitud máxima:"), self._max_chars, spacing=6),
                 vbox(_field("Cola máxima:"), self._max_queue, spacing=6), spacing=12),
            hbox(_field("Leer quién lo escribió («Pepe dice: …»)"), None, self._read_name),
            label(f"Los moderadores pueden saltar el mensaje con !{SKIP_COMMAND}. No se leen los enlaces, ni los "
                  "mensajes que bloquean los filtros; si sancionan a alguien, su mensaje sale de la cola.",
                  color=t.TEXT_3, size=11, wrap=True),
            spacing=10, margins=(22, 18, 22, 18),
        ))

    def _build_voice(self) -> QFrame:
        cfg = self.svc.config
        self._voice = QComboBox()
        self._voice.addItem(cfg.voice, cfg.voice)
        self._voice.currentIndexChanged.connect(self._save_voice)
        self._voice_msg = label("", color=t.RED, size=11, wrap=True)
        self._rate_lbl = label("", color=t.TEXT, size=12, mono=True, bold=True)
        self._volume_lbl = label("", color=t.TEXT, size=12, mono=True, bold=True)
        self._rate = self._slider(MIN_RATE, MAX_RATE, cfg.rate)
        self._volume = self._slider(0, 100, cfg.volume)
        self._update_slider_labels()
        self._test = QLineEdit(DEFAULT_TEST)
        self._test.returnPressed.connect(self._preview)
        return frame("card", vbox(
            hbox(icon_label("volume-2", t.ACCENT_SOFT, 16), label("VOZ", "sectionTitle"), None, spacing=8),
            self._voice, self._voice_msg,
            hbox(_field("Velocidad"), None, self._rate_lbl), self._rate,
            hbox(_field("Volumen"), None, self._volume_lbl), self._volume,
            _field("Probar:"),
            hbox(self._test, button("Probar", "play", "primary", self._preview), spacing=8),
            label("Las voces son del servicio de Microsoft Edge: gratis, pero necesitan internet.",
                  color=t.TEXT_3, size=11, wrap=True),
            spacing=10, margins=(22, 18, 22, 18),
        ))

    def _slider(self, low: int, high: int, value: int) -> QSlider:
        s = QSlider(Qt.Horizontal)
        s.setRange(low, high)
        s.setValue(value)  # antes de conectar: crear la página no debe guardar nada
        s.valueChanged.connect(lambda _v: self._update_slider_labels())
        s.sliderReleased.connect(self._save_sliders)
        s.valueChanged.connect(lambda _v, sl=s: None if sl.isSliderDown() else self._save_sliders())
        return s

    def _build_output(self) -> QFrame:
        self._device = QComboBox()
        self._device.currentIndexChanged.connect(self._save_device)
        return frame("card", vbox(
            hbox(icon_label("audio-lines", t.ACCENT_SOFT, 16), label("SALIDA DE AUDIO", "sectionTitle"), None, spacing=8),
            self._device,
            label("Elige un canal de Wave Link o un cable virtual para separar el TTS en OBS. "
                  "Se aplica desde el siguiente mensaje.", color=t.TEXT_3, size=11, wrap=True),
            spacing=10, margins=(22, 18, 22, 18),
        ))

    # ------------------------------------------------------------- acciones
    def _save_command(self) -> None:
        if self._command.text().strip().removeprefix("!").lower() == self.svc.config.command:
            self._command.setText(self.svc.config.command)
            return
        try:
            name = self.svc.set_command(self._command.text())
        except ValueError as exc:
            self._command_msg.setText(f"<span style='color:{t.RED}'>{exc}</span>")
            return
        self._command.setText(name)
        self._command_msg.setText(f"<span style='color:{t.GREEN}'>Guardado: ahora se usa !{name}</span>")

    def _save_voice(self) -> None:
        voice = self._voice.currentData()
        if voice and voice != self.svc.config.voice:
            self.svc.update(voice=voice)

    def _save_sliders(self) -> None:
        cfg = self.svc.config
        if (self._rate.value(), self._volume.value()) != (cfg.rate, cfg.volume):
            self.svc.update(rate=self._rate.value(), volume=self._volume.value())

    def _update_slider_labels(self) -> None:
        self._rate_lbl.setText(f"{self._rate.value():+d}%")
        self._volume_lbl.setText(f"{self._volume.value()}%")

    def _save_device(self) -> None:
        device = self._device.currentData()
        if device is not None and device != self.svc.config.output_device:
            self.svc.update(output_device=device)

    def _preview(self) -> None:
        if not self.svc.preview(self._test.text()):
            self._voice_msg.setText("Escribe algo legible para probar la voz.")
        else:
            self._voice_msg.clear()

    def _fill_devices(self) -> None:
        wanted = self.svc.config.output_device
        self._device.blockSignals(True)
        self._device.clear()
        self._device.addItem("Predeterminada de Windows", "")
        for dev_id, name in output_devices():
            self._device.addItem(name, dev_id)
        index = self._device.findData(wanted)
        if index < 0:  # desconectado: se conserva la elección por si vuelve
            self._device.addItem("Dispositivo no conectado (suena por la predeterminada)", wanted)
            index = self._device.count() - 1
        self._device.setCurrentIndex(index)
        self._device.blockSignals(False)

    # ---------------------------------------------------------------- voces
    def showEvent(self, event) -> None:  # noqa: N802 (Qt)
        super().showEvent(event)
        if not self._voices_loaded:  # la lista se pide a internet: solo al abrir la página
            self._voices_loaded = True
            asyncio.ensure_future(self._load_voices())

    async def _load_voices(self) -> None:
        try:
            voices = await self.svc.list_voices()
        except Exception as exc:  # sin internet: se queda la voz guardada, se reintenta al volver
            self._voices_loaded = False
            self._voice_msg.setText("No se pudo cargar la lista de voces. ¿Hay internet?")
            log.warning("TTS: no se pudo cargar la lista de voces (%s)", type(exc).__name__)
            return
        wanted = self.svc.config.voice
        self._voice.blockSignals(True)
        self._voice.clear()
        for v in voices:
            self._voice.addItem(v.label, v.name)
        index = self._voice.findData(wanted)
        if index < 0:
            self._voice.insertItem(0, wanted, wanted)
            index = 0
        self._voice.setCurrentIndex(index)
        self._voice.blockSignals(False)
        self._voice_msg.clear()

    # ------------------------------------------------------------- refresco
    def reload(self) -> None:
        svc = self.svc
        current = svc.current
        if current is not None:
            self._pill.set("HABLANDO", t.GREEN)
        elif svc.config.enabled:
            self._pill.set("EN ESPERA", t.ACCENT_SOFT)
        else:
            self._pill.set("DESACTIVADO", t.TEXT_3)
        text = current.text if current else f"Los viewers lo usan con !{svc.config.command} mensaje"
        self._now._full = text  # noqa: SLF001 (ElidedLabel)
        self._now.setToolTip(text)
        self._now.setText(self._now.fontMetrics().elidedText(text, Qt.ElideRight, max(40, self._now.width())))
        self._error.setText(svc.last_error)
        self._error.setVisible(bool(svc.last_error))
        pending = svc.pending
        self._queue_title.setText(f"EN COLA ({len(pending)})")
        clear_layout(self._queue_list)
        for item in pending:
            self._queue_list.addWidget(QueueRow(self, item))
        if not pending:
            self._queue_list.addWidget(label("No hay mensajes pendientes.", color=t.TEXT_3, size=12))
