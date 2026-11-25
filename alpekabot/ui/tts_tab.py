"""Página «Texto a voz»: modos (comando y canje, o todo el chat), límites, voz, salida de audio y cola."""

from __future__ import annotations

import asyncio
import logging

from PySide6.QtCore import Qt
from PySide6.QtMultimedia import QMediaDevices
from PySide6.QtWidgets import QComboBox, QFrame, QLineEdit, QMessageBox, QSlider, QSpinBox, QWidget

from ..core.models import Permission, Platform
from ..features.reactions import RewardLink
from ..features.tts import (
    MAX_CHARS,
    MAX_COOLDOWN_S,
    MAX_QUEUE,
    MAX_RATE,
    MIN_CHARS,
    MIN_QUEUE,
    MIN_RATE,
    MODE_CHAT,
    MODE_COMMAND,
    MODE_REWARD,
    SKIP_COMMAND,
    TtsItem,
    TtsService,
    reward_links,
)
from . import theme as t
from .commands_tab import ElidedLabel, scroll_page
from .tts_player import output_devices
from .vtube_tab import RewardDialog, RewardHost, RewardPicker
from .widgets import Pill, Toggle, button, clear_layout, frame, hbox, icon_button, icon_label, label, vbox

log = logging.getLogger(__name__)

MODE_LABELS = {  # en este orden se muestran los interruptores
    MODE_COMMAND: "Comando (!{command} mensaje)",
    MODE_REWARD: "Canje de puntos del canal",
    MODE_CHAT: "Leer todo el chat (va solo)",
}
CHAT_WARNING = (
    "Con este modo se lee en voz alta CUALQUIER mensaje del chat (menos los comandos !algo).\n\n"
    "Cualquier viewer, también un troll o un raid hostil, podrá hacer hablar al bot en tu directo. "
    "Los filtros de moderación siguen funcionando y los mods pueden callarlo con !skiptts, pero "
    "lo que no bloqueen los filtros se oirá.\n\n¿Activar «Leer todo el chat»?"
)
REWARD_SUBTITLE = ("Un premio de puntos de canal para el TTS: el viewer escribe el mensaje al canjearlo. "
                   "Si no llega a leerse, la app devuelve los puntos.")

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


class TtsTab(QWidget, RewardHost):
    title = "Texto a voz (TTS)"
    icon = "audio-lines"

    def __init__(self, service: TtsService, bus, bot=None, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("page")
        self.svc = service
        # RewardPicker usa _bot y _repo de quien lo contiene (como el editor de reacciones)
        self._bot = bot
        self._repo = bot.reactions.repo if bot is not None else None
        self._rewards_loaded = False
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
        bus.commands_changed.connect(self.reload)  # el aviso de !tts apagado en Comandos
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
        self._skip_emotes = Toggle(cfg.skip_emotes)
        self._skip_emotes.toggled.connect(lambda on: self.svc.update(skip_emotes=on))

        self._mode_toggles: dict[str, Toggle] = {}
        self._mode_labels: dict[str, QWidget] = {}
        mode_rows = []
        for mode, text in MODE_LABELS.items():
            toggle = Toggle(self.svc.has_mode(mode))
            toggle.toggled.connect(lambda on, m=mode: self._change_mode(m, on))
            self._mode_toggles[mode] = toggle
            self._mode_labels[mode] = _field(text.format(command=cfg.command))
            mode_rows.append(hbox(self._mode_labels[mode], None, toggle))
        self._mode_msg = label("", color=t.RED, size=11, wrap=True)
        self._mode_warn = label("", color=t.AMBER, size=12, wrap=True)
        self._mode_warn.setTextFormat(Qt.PlainText)

        self._command_box = QWidget()
        self._command_box.setLayout(vbox(
            _field("Comando en Twitch y Kick:"), hbox(label("!", size=16, bold=True, mono=True), self._command, spacing=6),
            self._command_msg, _field("Pueden usarlo:"), self._perm, spacing=10, margins=(0, 0, 0, 0),
        ))
        self._chat_warning = label("⚠ Se lee en voz alta todo el chat menos los comandos (!algo). Cualquiera puede "
                                   "hacer hablar al bot: vigila el chat.", color=t.AMBER, size=12, wrap=True)
        self._cooldown_box = QWidget()
        self._cooldown_box.setLayout(vbox(_field("Cooldown por usuario:"), self._cooldown, spacing=6,
                                          margins=(0, 0, 0, 0)))
        return frame("card", vbox(
            hbox(icon_label("terminal", t.ACCENT_SOFT, 16), label("MODO Y LÍMITES", "sectionTitle"), None, spacing=8),
            _field("Qué se lee:"), *mode_rows, self._mode_msg, self._mode_warn, self._chat_warning,
            self._command_box, self._build_rewards(),
            hbox(self._cooldown_box,
                 vbox(_field("Longitud máxima:"), self._max_chars, spacing=6),
                 vbox(_field("Cola máxima:"), self._max_queue, spacing=6), spacing=12),
            hbox(_field("Leer quién lo escribió («Pepe dice: …»)"), None, self._read_name),
            hbox(_field("No leer los emotes de Twitch y Kick"), None, self._skip_emotes),
            label(f"Los moderadores pueden saltar el mensaje con !{SKIP_COMMAND}. No se leen los enlaces, ni los "
                  "mensajes que bloquean los filtros; si sancionan a alguien, su mensaje sale de la cola.",
                  color=t.TEXT_3, size=11, wrap=True),
            spacing=10, margins=(22, 18, 22, 18),
        ))

    def _build_rewards(self) -> QWidget:
        links = reward_links(self.svc.config)
        self.pickers: dict[Platform, RewardPicker] = {}
        self._reward_hint = label("", color=t.TEXT_3, size=12, wrap=True)
        self._reward_hint.setTextFormat(Qt.PlainText)  # los errores de las plataformas no son HTML
        box = QWidget()
        if self._bot is None:  # sin bot (pruebas): no hay plataformas que consultar
            box.setLayout(vbox(self._reward_hint, margins=(0, 0, 0, 0)))
            self._reward_hint.setText("Los premios no están disponibles.")
            self._rewards_box = box
            return box
        for p in Platform:
            link = links.get(p)
            picker = RewardPicker(self, p, RewardLink(p, link["id"], link.get("title", "")) if link else None)
            picker.combo.currentIndexChanged.connect(lambda _i, pk=picker: self._save_reward(pk))
            self.pickers[p] = picker
        reload_rewards = icon_button("repeat", "Volver a cargar los premios",
                                     lambda: asyncio.ensure_future(self.load_rewards()), t.TEXT_2, 16, "iconBox")
        hint_row = hbox(self._reward_hint, reload_rewards, spacing=8)
        hint_row.setStretchFactor(self._reward_hint, 1)
        box.setLayout(vbox(_field("Premio que activa el TTS:"), *self.pickers.values(), hint_row,
                           label("Crea el premio con + para que pida el mensaje y se devuelvan los puntos si no se "
                                 "lee (texto vacío, sanción, fallo o demasiados canjes en cola). Los canjes no "
                                 "cuentan para la cola máxima. Con un premio ya existente solo se lee el texto; "
                                 "el premio debe pedir texto al viewer.", color=t.TEXT_3, size=11, wrap=True),
                           spacing=6, margins=(0, 0, 0, 0)))
        self._rewards_box = box
        return box

    # ------------------------------------------------------- modo y premios
    def _change_mode(self, mode: str, on: bool) -> None:
        if mode == MODE_CHAT and on:
            answer = QMessageBox.warning(self, "Leer todo el chat", CHAT_WARNING,
                                         QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
            if answer != QMessageBox.Yes:
                self._sync_mode()  # el interruptor vuelve a como estaba
                return
        try:
            self.svc.set_mode(mode, on)
        except ValueError as exc:
            self._mode_msg.setText(str(exc))
        else:
            self._mode_msg.setText("")
            if mode == MODE_REWARD and on and not self._rewards_loaded:
                asyncio.ensure_future(self.load_rewards())
        self._sync_mode()

    def _sync_mode(self) -> None:
        svc, cfg = self.svc, self.svc.config
        chat = svc.has_mode(MODE_CHAT)
        for mode, toggle in self._mode_toggles.items():
            toggle.blockSignals(True)
            toggle.setChecked(svc.has_mode(mode))
            toggle.blockSignals(False)
            toggle.setEnabled(mode == MODE_CHAT or not chat)  # «Leer todo» va solo
        self._mode_labels[MODE_COMMAND].setText(MODE_LABELS[MODE_COMMAND].format(command=cfg.command))
        self._command_box.setVisible(svc.has_mode(MODE_COMMAND))
        self._chat_warning.setVisible(chat)
        self._rewards_box.setVisible(svc.has_mode(MODE_REWARD))
        # los premios ya tienen su cooldown en la plataforma
        self._cooldown_box.setVisible(not svc.has_mode(MODE_REWARD) or svc.has_mode(MODE_COMMAND))
        warnings = []
        if svc.has_mode(MODE_REWARD) and not reward_links(cfg):
            warnings.append("⚠ El modo canje está encendido, pero no hay ningún premio elegido.")
        if svc.has_mode(MODE_COMMAND) and not svc.command_enabled:
            warnings.append(f"⚠ !{cfg.command} está apagado en Comandos → Comandos del sistema: nadie podrá usarlo.")
        self._mode_warn.setText("\n".join(warnings))
        self._mode_warn.setVisible(bool(warnings))

    def spawn(self, coro) -> asyncio.Future:  # lo usa RewardHost
        return asyncio.ensure_future(coro)

    def _save_reward(self, picker: RewardPicker) -> None:
        reward = picker.selected()
        self.svc.set_reward(picker.platform, reward.id if reward else "", reward.title if reward else "")

    def open_reward_dialog(self, platform: Platform) -> RewardDialog:
        dlg = RewardDialog(self._bot, platform, self, subtitle=REWARD_SUBTITLE, input_required=True)
        dlg.title_edit.setPlaceholderText("Mensaje por TTS")
        dlg.prompt.setPlaceholderText("Escribe lo que dirá el bot")
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        dlg.created.connect(self._on_reward_created)
        dlg.open()
        return dlg

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
        if self.svc.has_mode(MODE_REWARD) and not self._rewards_loaded:
            asyncio.ensure_future(self.load_rewards())
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
        self._sync_mode()
        if svc.has_mode(MODE_CHAT):
            idle = "Se lee todo el chat (menos los comandos)"
        else:
            ways = {MODE_COMMAND: f"con !{svc.config.command} mensaje", MODE_REWARD: "canjeando el premio del TTS"}
            idle = "Los viewers lo usan " + " o ".join(w for m, w in ways.items() if svc.has_mode(m))
        text = current.text if current else idle
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
