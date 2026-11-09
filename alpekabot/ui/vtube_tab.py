"""Página «VTube Studio»: conexión y reacciones del avatar (los premios se gestionan en su editor)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QSpinBox,
    QWidget,
)

from ..core.models import EventKind, Platform, Reward
from ..features.reactions import (
    EXPRESSION,
    HOTKEY,
    MAX_NAME_LEN,
    MAX_STEP_S,
    MAX_STEPS,
    WAIT,
    Reaction,
    RewardLink,
    Step,
)
from ..integrations.vtube_studio import VTSError, VTSState, VTSStatus
from ..platforms.base import PlatformError
from . import theme as t
from .commands_tab import ElidedLabel, VariablesBar, platform_line, scroll_page
from .widgets import (
    Dot,
    ModalDialog,
    Pill,
    PlatformPicker,
    Toggle,
    button,
    clear_layout,
    frame,
    hbox,
    icon_button,
    icon_label,
    label,
    section_header,
    vbox,
)

log = logging.getLogger(__name__)

REACTION_VARIABLES = ["$user", "$amount", "$reward", "$input", "$channel", "$platform", "$uptime", "$game", "$title",
                      "$random(1,100)", "$pick(a|b|c)"]
TRIGGER_ICONS = {
    EventKind.REWARD: "star", EventKind.FOLLOW: "user-plus", EventKind.SUB: "crown", EventKind.RESUB: "repeat",
    EventKind.GIFT: "gift", EventKind.BITS: "gem", EventKind.RAID: "users",
}
# Disparadores con cantidad mínima: (etiqueta del campo, sufijo)
MIN_AMOUNTS = {
    EventKind.GIFT: ("Mínimo de subs en el regalo", " subs"),
    EventKind.BITS: ("Mínimo de bits o kicks", ""),
    EventKind.RAID: ("Mínimo de espectadores", " espectadores"),
}
STATE_COLORS = {
    VTSState.OFF: t.TEXT_3, VTSState.CONNECTING: t.AMBER, VTSState.NEEDS_AUTH: t.AMBER, VTSState.CONNECTED: t.GREEN,
}
MAX_REWARD_TITLE = 45
PLATFORM_COLORS = {Platform.TWITCH: t.ACCENT_SOFT, Platform.KICK: t.KICK}


def trigger_text(r: Reaction) -> str:
    """«Canje de «Saludo»», «Subs regaladas: 5 o más», «Follow»…"""
    if r.trigger is EventKind.REWARD:
        if len(r.rewards) > 1:
            return "Canje: " + " · ".join(f"{x.platform.label} «{x.title or 'premio'}»" for x in r.rewards)
        return f"Canje de «{(r.rewards[0].title if r.rewards else '') or 'premio'}»"
    if r.trigger in MIN_AMOUNTS and r.min_amount > 0:
        return f"{r.trigger.label}: {r.min_amount} o más"
    return r.trigger.label


def steps_text(r: Reaction) -> str:
    return " → ".join(s.describe() for s in r.steps) or "Solo mensaje al chat"


def why_cannot_test(bot, r: Reaction) -> str:
    """Por qué no se puede probar una reacción ("" si se puede)."""
    if not r.steps:
        return "Esta reacción no tiene pasos: solo envía un mensaje al chat, y la prueba no lo envía."
    if not bot.vtube.is_connected:
        return "Para probarla, VTube Studio tiene que estar conectado y autorizado."
    if any(s.kind != WAIT and not s.ref for s in r.steps):
        return "Hay un paso sin hotkey o expresión elegida."
    return ""


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _elided(text: str, css: str) -> ElidedLabel:
    """Texto de una línea, sin interpretar como HTML, que se recorta con «…» si no cabe."""
    lbl = ElidedLabel(text)
    lbl.setTextFormat(Qt.PlainText)
    lbl.setStyleSheet(css)
    return lbl


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


# ======================================================================= filas
class ReactionRow(QFrame):
    def __init__(self, page: "VTubeTab", r: Reaction) -> None:
        super().__init__()
        self.setObjectName("row")
        color = t.TEXT if r.enabled else t.TEXT_3
        left = QWidget()
        left.setFixedWidth(250)
        name = _elided(r.name, f"font-size: 15px; font-weight: 600; color: {color};")
        left.setLayout(vbox(
            hbox(icon_label(TRIGGER_ICONS[r.trigger], t.ACCENT_SOFT if r.enabled else t.TEXT_4, 15), name, spacing=8),
            platform_line(r.platforms), spacing=6,
        ))
        steps = _elided(steps_text(r), f"font-size: 14px; color: {color};")
        details = [trigger_text(r)]
        if r.cooldown_s:
            details.append(f"cooldown {r.cooldown_s} s")
        if r.message and r.steps:
            details.append("con mensaje al chat")
        # Recortables: con la ventana estrecha no deben empujar los botones fuera de la fila
        meta = _elided("  ·  ".join(details), f"font-family: '{t.MONO}'; font-size: 11px; color: {t.TEXT_3};")
        middle = QWidget()
        middle.setLayout(vbox(steps, meta, spacing=6))
        toggle = Toggle(r.enabled)
        toggle.setToolTip("Activar / pausar")
        toggle.toggled.connect(lambda on: page.repo.update(replace(r, enabled=on)))
        test = icon_button("play", "Probar en el avatar (sin mensaje al chat)", lambda: page.test_reaction(r),
                           t.GREEN, 17)
        edit = icon_button("pencil", "Editar", lambda: page.open_reaction(r), t.TEXT_2, 17)
        lay = hbox(left, middle, toggle, 6, test, edit, spacing=12, margins=(18, 14, 14, 14))
        lay.setStretchFactor(middle, 1)
        self.setLayout(lay)


# ====================================================================== página
class VTubeTab(QWidget):
    title = "VTube Studio"
    icon = "smile"

    def __init__(self, bot, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("page")
        self.bot = bot
        self.repo = bot.reactions.repo
        self._applying = False

        new = button("Nueva Reacción", "plus", "primary", lambda: self.open_reaction(None))
        self.actions = QWidget()
        self.actions.setLayout(hbox(new, spacing=10))

        # ------------------------------------------------------------- conexión
        cfg = bot.config.vtube
        self._dot = Dot(t.TEXT_3, 10)
        self._state = label("", size=15, bold=True)
        self._detail = label("", color=t.TEXT_3, size=12, wrap=True)
        self.auth_button = button("Autorizar", "shield-check", "primary", self._on_authorize,
                                  tooltip="VTube Studio mostrará un aviso para permitir el plugin AlpekaBOT")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(cfg.port)
        self.port.setFixedWidth(90)
        self.port.setObjectName("mono")
        self.port.setToolTip("Puerto de la API de VTube Studio (por defecto 8001)")
        self.port.editingFinished.connect(self._on_connection_changed)
        self.enabled_toggle = Toggle(cfg.enabled)
        self.enabled_toggle.setToolTip("Conectar con VTube Studio (reintenta solo si está cerrado)")
        self.enabled_toggle.toggled.connect(self._on_connection_changed)
        texts = vbox(hbox(self._dot, self._state, None, spacing=10), self._detail, spacing=4)
        status = hbox(texts, self.auth_button, 8, label("Puerto", color=t.TEXT_3, size=12), self.port, 8,
                      label("Activar", color=t.TEXT_3, size=12), self.enabled_toggle,
                      spacing=10, margins=(18, 14, 18, 14))
        status.setStretchFactor(texts, 1)
        status_card = frame("card", status)

        self.notice = label("", color=t.TEXT_2, size=12, wrap=True)
        self.notice.hide()

        # ------------------------------------------------------------ reacciones
        reactions_head, self._reactions_count = section_header("sparkles", "Reacciones")
        info = label("Se ejecutan en cola, una detrás de otra, con canjes de premios, follows, subs, bits, kicks y "
                     "raids. Si VTube Studio está cerrado se descartan (un premio creado por la app devuelve los "
                     "puntos).", color=t.TEXT_3, size=12, wrap=True)
        self._reactions = vbox(spacing=10)
        self._reactions_empty = label("Todavía no hay reacciones. Crea una con «Nueva Reacción».", color=t.TEXT_3,
                                      size=13)

        content = vbox(status_card, self.notice, 6, reactions_head, info, self._reactions, self._reactions_empty, None,
                       spacing=14, margins=(28, 20, 28, 24))
        self.setLayout(vbox(scroll_page(content), spacing=0))

        bot.bus.vts_status.connect(self._on_status)
        bot.bus.reactions_changed.connect(self.reload)
        self._on_status(bot.vtube.status)
        self.reload()

    # ------------------------------------------------------------------ estado
    def _on_status(self, st: VTSStatus) -> None:
        self._dot.set_color(STATE_COLORS[st.state])
        text = st.state.label
        if st.state is VTSState.CONNECTED and st.model:
            text += f" · {st.model}"
        self._state.setText(_escape(text))
        detail = st.detail or {
            VTSState.OFF: "Activa la conexión para que tu avatar reaccione al canal.",
            VTSState.CONNECTED: "Tu avatar ya puede reaccionar.",
        }.get(st.state, "")
        self._detail.setText(_escape(detail))
        self._detail.setVisible(bool(detail))
        self.auth_button.setVisible(st.state is VTSState.NEEDS_AUTH)
        if st.state is VTSState.CONNECTED and self.notice.property("error"):
            self._hide_notice()

    def _show_notice(self, text: str, error: bool = False) -> None:
        self.notice.setProperty("error", error)
        self.notice.setStyleSheet(f"font-size: 12px; color: {t.RED if error else t.TEXT_2};")
        self.notice.setText(_escape(text))
        self.notice.show()

    def _hide_notice(self) -> None:
        self.notice.setProperty("error", False)
        self.notice.hide()

    def _on_connection_changed(self, *_) -> None:
        asyncio.ensure_future(self._apply_connection())

    async def _apply_connection(self) -> None:
        """Aplica el interruptor y el puerto. Si cambian mientras tanto, vuelve a aplicarlos."""
        if self._applying:
            return
        self._applying = True
        cfg = self.bot.config.vtube
        try:
            while (self.enabled_toggle.isChecked(), self.port.value()) != (cfg.enabled, cfg.port):
                await self.bot.set_vtube(self.enabled_toggle.isChecked(), self.port.value())
        except OSError:
            log.exception("No se pudo guardar la configuración de VTube Studio")
            self._show_notice("No se pudo guardar la configuración (mira la consola).", error=True)
        finally:
            self._applying = False

    def _on_authorize(self) -> None:
        asyncio.ensure_future(self._authorize())

    async def _authorize(self) -> None:
        self.auth_button.setEnabled(False)
        self.auth_button.setText("Acepta el aviso en VTube Studio…")
        try:
            await self.bot.vtube.authorize()
        except VTSError as exc:
            self._show_notice(str(exc), error=True)
        finally:
            self.auth_button.setEnabled(True)
            self.auth_button.setText("Autorizar")

    # -------------------------------------------------------------- reacciones
    def reload(self) -> None:
        items = self.repo.list_all()
        self._reactions_count.setText(_count(len(items), "reacción", "reacciones"))
        clear_layout(self._reactions)
        for r in items:
            self._reactions.addWidget(ReactionRow(self, r))
        self._reactions_empty.setVisible(not items)

    def open_reaction(self, r: Reaction | None) -> None:
        # open() y no exec(): exec() crea un bucle anidado que frena las tareas asyncio (qasync)
        dlg = ReactionDialog(self.bot, r, self)
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        dlg.open()

    def test_reaction(self, r: Reaction) -> None:
        problem = why_cannot_test(self.bot, r)
        if problem:
            self._show_notice(problem, error=True)
            return
        self.bot.reactions.test(r)
        self._show_notice(f"Probando «{r.name}»: mira tu avatar. Si no pasa nada, revisa «Consola & Logs».")


# ===================================================================== pasos
class StepRow(QFrame):
    """Una fila del editor de pasos: hotkey, expresión o espera."""

    def __init__(self, dialog: "ReactionDialog", step: Step) -> None:
        super().__init__()
        self.setObjectName("cardInner")
        self.kind = step.kind
        self._saved_ref = step.ref
        self._names = {step.ref: step.name} if step.ref else {}
        self.combo = QComboBox()
        self.mode = QComboBox()
        self.seconds = QDoubleSpinBox()
        self.seconds.setRange(0, MAX_STEP_S)
        self.seconds.setSingleStep(0.5)
        self.seconds.setDecimals(1)
        self.seconds.setSuffix(" s")
        self.seconds.setFixedWidth(90)
        self.seconds.setObjectName("mono")
        self.seconds.setValue(step.seconds)

        icon, title = {HOTKEY: ("zap", "Hotkey"), EXPRESSION: ("smile", "Expresión"),
                       WAIT: ("timer", "Esperar")}[step.kind]
        name = label(title, size=13, bold=True)
        name.setFixedWidth(72)
        lay = hbox(icon_label(icon, t.ACCENT_SOFT, 15), name, spacing=8, margins=(12, 8, 8, 8))
        if step.kind == WAIT:
            lay.addWidget(self.seconds)
            lay.addStretch(1)
        else:
            lay.addWidget(self.combo, 1)
            self.set_options([])
        if step.kind == EXPRESSION:
            self.mode.addItem("Poner", True)
            self.mode.addItem("Quitar", False)
            self.mode.setCurrentIndex(0 if step.active else 1)
            self.mode.setFixedWidth(90)
            self.mode.currentIndexChanged.connect(self._sync_mode)
            self.seconds.setToolTip("Cuánto dura la expresión. 0 = se queda puesta")
            lay.addWidget(self.mode)
            lay.addWidget(self.seconds)
            self._sync_mode()
        lay.addWidget(icon_button("chevron-up", "Subir", lambda: dialog.move_step(self, -1), t.TEXT_2, 15))
        lay.addWidget(icon_button("chevron-down", "Bajar", lambda: dialog.move_step(self, 1), t.TEXT_2, 15))
        lay.addWidget(icon_button("x", "Quitar paso", lambda: dialog.remove_step(self), t.TEXT_2, 15))
        self.setLayout(lay)

    def _sync_mode(self, *_) -> None:
        self.seconds.setEnabled(bool(self.mode.currentData()))

    def set_options(self, options: list[tuple[str, str]]) -> None:
        """(ref, nombre). Lo ya elegido se conserva aunque el modelo actual no lo tenga."""
        current = self.combo.currentData() or self._saved_ref
        self._names.update(options)
        self.combo.clear()
        if current and current not in {ref for ref, _ in options}:
            self.combo.addItem(f"{self._names.get(current) or current} (no disponible ahora)", current)
        for ref, name in options:
            self.combo.addItem(name or ref, ref)
        index = self.combo.findData(current) if current else -1
        if index >= 0:
            self.combo.setCurrentIndex(index)

    def value(self) -> Step:
        if self.kind == WAIT:
            return Step(WAIT, seconds=self.seconds.value())
        ref = self.combo.currentData() or ""
        name = self._names.get(ref, "")
        if self.kind == HOTKEY:
            return Step(HOTKEY, ref, name)
        active = bool(self.mode.currentData())
        return Step(EXPRESSION, ref, name, self.seconds.value() if active else 0, active)


# =================================================================== diálogos
class RewardPicker(QWidget):
    """Premio de una plataforma para una reacción de canje, con crear y borrar."""

    def __init__(self, dialog: "ReactionDialog", platform: Platform, link: RewardLink | None) -> None:
        super().__init__()
        self.platform = platform
        self.rewards: dict[str, Reward] = {}
        self._connected = False
        self.combo = QComboBox()
        self.combo.addItem(f"Sin premio en {platform.label}", "")
        self.combo.currentIndexChanged.connect(self._sync)
        self.create_button = icon_button("plus", "", lambda: dialog.open_reward_dialog(platform), t.TEXT, 16, "iconBox")
        self.delete_button = icon_button("trash-2", f"Borrar este premio de {platform.label}",
                                         lambda: dialog.confirm_delete_reward(self), t.TEXT_2, 16, "iconBox")
        policy = self.delete_button.sizePolicy()
        policy.setRetainSizeWhenHidden(True)  # así los selectores de las dos plataformas quedan alineados
        self.delete_button.setSizePolicy(policy)
        badge = Pill(platform.label, PLATFORM_COLORS[platform], size=10)
        badge.setFixedWidth(64)
        lay = hbox(badge, self.combo, self.create_button, self.delete_button, spacing=8)
        lay.setStretchFactor(self.combo, 1)
        self.setLayout(lay)
        if link:
            self.add(Reward(platform, link.id, link.title, managed=dialog._repo.is_managed(platform, link.id)))
            self.select(link.id)
        self.set_connected(dialog._bot.platforms[platform].is_connected)

    def add(self, reward: Reward) -> None:
        text = reward.title + (f" ({reward.cost} puntos)" if reward.cost else "")
        index = self.combo.findData(reward.id)
        self.rewards[reward.id] = reward
        if index >= 0:
            self.combo.setItemText(index, text)
        else:
            self.combo.addItem(text, reward.id)
        self._sync()

    def remove(self, reward_id: str) -> None:
        self.rewards.pop(reward_id, None)
        index = self.combo.findData(reward_id)
        if index >= 0:
            if self.combo.currentIndex() == index:
                self.combo.setCurrentIndex(0)  # si no, Qt elegiría solo el premio de al lado
            self.combo.removeItem(index)

    def select(self, reward_id: str) -> None:
        index = self.combo.findData(reward_id)
        if index >= 0:
            self.combo.setCurrentIndex(index)

    def selected(self) -> Reward | None:
        return self.rewards.get(self.combo.currentData() or "")

    def set_connected(self, connected: bool) -> None:
        self._connected = connected
        self.create_button.setEnabled(connected)
        self.create_button.setToolTip(f"Crear un premio en {self.platform.label}" if connected
                                      else f"Conecta {self.platform.label} para crear premios")
        self._sync()

    def _sync(self, *_) -> None:
        reward = self.selected()
        self.delete_button.setVisible(bool(reward and reward.managed))  # solo se pueden borrar los de la app
        self.delete_button.setEnabled(self._connected)


class _AsyncDialog(ModalDialog):
    """Modal cuyas tareas en segundo plano se cancelan al cerrarlo (sus widgets dejan de existir)."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._tasks: list[asyncio.Future] = []

    def spawn(self, coro) -> asyncio.Future:
        task = asyncio.ensure_future(coro)
        self._tasks.append(task)
        return task

    def done(self, result: int) -> None:
        for task in self._tasks:
            task.cancel()
        super().done(result)


class ReactionDialog(_AsyncDialog):
    def __init__(self, bot, reaction: Reaction | None, parent=None) -> None:
        super().__init__("Editar Reacción" if reaction else "Crear Nueva Reacción",
                         "Qué hace tu avatar cuando llega un canje o un evento del canal.", "sparkles", parent,
                         width=820)
        self._bot = bot
        self._repo = bot.reactions.repo
        self._reaction = reaction
        x = reaction or Reaction(name="", trigger=EventKind.REWARD)
        self.rows: list[StepRow] = []
        self._hotkeys: list[tuple[str, str]] = []
        self._expressions: list[tuple[str, str]] = []

        form = vbox(spacing=14)
        self.name = QLineEdit(x.name)
        self.name.setMaxLength(MAX_NAME_LEN)
        self.name.setPlaceholderText("Saludo al canjear")
        self.name.setMinimumHeight(40)
        form.addLayout(vbox(self.field_label("Nombre", True), self.name, spacing=6))

        self.trigger = QComboBox()
        for kind in EventKind:
            self.trigger.addItem(t.icon(TRIGGER_ICONS[kind], t.ACCENT_SOFT, 16), kind.label, kind.value)
        self.trigger.setCurrentIndex(self.trigger.findData(x.trigger.value))
        self.trigger.currentIndexChanged.connect(self._sync_trigger)
        form.addLayout(vbox(self.field_label("Cuándo salta", True), self.trigger, spacing=6))

        # premios (solo canjes): uno por plataforma, como mínimo uno
        links = {link.platform: link for link in x.rewards}
        self.pickers = {p: RewardPicker(self, p, links.get(p)) for p in Platform}
        self._reward_hint = label("", color=t.TEXT_3, size=12, wrap=True)
        reload_rewards = icon_button("repeat", "Volver a cargar los premios",
                                     lambda: self.spawn(self.load_rewards()), t.TEXT_2, 16, "iconBox")
        hint_row = hbox(self._reward_hint, reload_rewards, spacing=8)
        hint_row.setStretchFactor(self._reward_hint, 1)
        self._reward_box = QWidget()
        self._reward_box.setLayout(vbox(self.field_label("Premios", True), *self.pickers.values(), hint_row, spacing=6))
        form.addWidget(self._reward_box)

        # cantidad mínima
        self.min_amount = QSpinBox()
        self.min_amount.setRange(0, 10_000_000)
        self.min_amount.setFixedWidth(200)
        self.min_amount.setObjectName("mono")
        self.min_amount.setValue(x.min_amount)
        self._min_label = label("", bold=True)
        self._min_box = QWidget()
        self._min_box.setLayout(vbox(self._min_label, hbox(self.min_amount, None), spacing=6))
        form.addWidget(self._min_box)

        # plataformas
        self.platforms = PlatformPicker(x.platforms)
        self._platforms_box = QWidget()
        self._platforms_box.setLayout(vbox(self.field_label("Plataformas"), self.platforms, spacing=6))
        form.addWidget(self._platforms_box)

        # pasos
        self.add_hotkey = button("Hotkey", "zap", slot=lambda: self.new_step(HOTKEY))
        self.add_expression = button("Expresión", "smile", slot=lambda: self.new_step(EXPRESSION))
        self.add_wait = button("Esperar", "timer", slot=lambda: self.new_step(WAIT))
        self._steps = vbox(spacing=8)
        self._steps_hint = label("", color=t.AMBER, size=12, wrap=True)
        self._steps_hint.hide()
        form.addLayout(vbox(
            hbox(self.field_label("Pasos del avatar"), label("Añadir:", color=t.TEXT_3, size=12), self.add_hotkey,
                 self.add_expression, self.add_wait, spacing=8),
            self._steps_hint, self._steps, spacing=8,
        ))
        for step in x.steps:
            self._add_row(step)

        # mensaje y límites
        self.message = QPlainTextEdit(x.message)
        self.message.setObjectName("mono")
        self.message.setPlaceholderText("Opcional. ¡Gracias $user por canjear $reward!")
        self.message.setFixedHeight(70)
        insert = VariablesBar(REACTION_VARIABLES, self._insert, hint="", title="Insertar:")
        form.addLayout(vbox(self.field_label("Mensaje al chat"), insert, self.message, spacing=8))

        self.cooldown = QSpinBox()
        self.cooldown.setRange(0, 86_400)
        self.cooldown.setSuffix(" s")
        self.cooldown.setFixedWidth(120)
        self.cooldown.setObjectName("mono")
        self.cooldown.setValue(x.cooldown_s)
        self.enabled = QCheckBox("Reacción activa")
        self.enabled.setChecked(x.enabled)
        form.addLayout(hbox(label("<b>Cooldown</b>"), self.cooldown, 24, self.enabled, None, spacing=10))
        form.addStretch(1)

        area = scroll_page(form)
        area.setMinimumHeight(440)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.body.addWidget(area)
        self.error = label("", color=t.RED, size=12, wrap=True)
        self.error.hide()
        self.body.addWidget(self.error)

        if reaction:
            self.footer.addWidget(button("Eliminar", "trash-2", "danger", self._delete, icon_color="#fca5a5"))
        self.footer.addWidget(button("Cancelar", slot=self.reject))
        self.footer.addStretch(1)
        self.footer.addWidget(button("Probar", "play", slot=self.test, icon_color=t.GREEN,
                                     tooltip="Ejecuta los pasos en el avatar sin guardar ni escribir en el chat"))
        self.footer.addWidget(button("Guardar Reacción", "check", "primary", self.save))

        self._sync_trigger()
        self._sync_step_buttons()
        self.spawn(self.load_rewards())
        self.spawn(self.load_avatar())

    # ------------------------------------------------------------ disparador
    def _kind(self) -> EventKind:
        return EventKind(self.trigger.currentData())

    def _sync_trigger(self, *_) -> None:
        kind = self._kind()
        self._reward_box.setVisible(kind is EventKind.REWARD)
        # Los premios elegidos ya deciden las plataformas; los raids solo existen en Twitch
        self._platforms_box.setVisible(kind not in (EventKind.REWARD, EventKind.RAID))
        self._min_box.setVisible(kind in MIN_AMOUNTS)
        if kind in MIN_AMOUNTS:
            text, suffix = MIN_AMOUNTS[kind]
            self._min_label.setText(text)
            self.min_amount.setSuffix(suffix)

    async def load_rewards(self) -> None:
        for picker in self.pickers.values():
            picker.set_connected(self._bot.platforms[picker.platform].is_connected)
        if not self._bot.any_connected:
            self._reward_hint.setText("Conecta Twitch o Kick para ver sus premios.")
            return
        self._reward_hint.setText("Cargando premios…")
        try:
            rewards, errors = await self._bot.list_rewards()
        except Exception:
            log.exception("Error cargando los premios")
            rewards, errors = [], ["Error inesperado al cargar los premios (mira la consola)."]
        for reward in rewards:
            self.pickers[reward.platform].add(reward)
        if errors:
            self._reward_hint.setText(_escape(" · ".join(errors)))
        elif not any(picker.rewards for picker in self.pickers.values()):
            self._reward_hint.setText("Todavía no hay premios: crea uno con el botón +.")
        else:
            self._reward_hint.setText("Los premios creados por la app devuelven los puntos si VTube Studio está cerrado.")

    def open_reward_dialog(self, platform: Platform) -> "RewardDialog":
        dlg = RewardDialog(self._bot, platform, self)
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        dlg.created.connect(self._on_reward_created)
        dlg.open()
        return dlg

    def _on_reward_created(self, reward: Reward) -> None:
        picker = self.pickers[reward.platform]
        picker.add(replace(reward, managed=True))
        picker.select(reward.id)

    def confirm_delete_reward(self, picker: RewardPicker) -> None:
        reward = picker.selected()
        if reward is None:
            return
        answer = QMessageBox.question(
            self, "Borrar premio",
            f"¿Borrar el premio «{reward.title}» de {reward.platform.label}?\n"
            "Las reacciones que lo usan dejarán de saltar en esa plataforma.",
        )
        if answer == QMessageBox.Yes:
            self.spawn(self.delete_reward(picker, reward))

    async def delete_reward(self, picker: RewardPicker, reward: Reward) -> None:
        try:
            await self._bot.delete_reward(reward.platform, reward.id)
        except PlatformError as exc:
            self._reward_hint.setText(_escape(str(exc)))
            return
        picker.remove(reward.id)

    # ----------------------------------------------------------------- pasos
    async def load_avatar(self) -> None:
        vtube = self._bot.vtube
        if not vtube.is_connected:
            self._show_steps_hint("VTube Studio no está conectado: ábrelo y autoriza la app para elegir hotkeys y "
                                  "expresiones de tu modelo.")
            return
        try:
            hotkeys, expressions = await asyncio.gather(vtube.hotkeys(), vtube.expressions())
        except VTSError as exc:
            self._show_steps_hint(_escape(str(exc)))
            return
        self._hotkeys = [(h.id, h.name) for h in hotkeys]
        self._expressions = [(e.file, e.name) for e in expressions]
        for row in self.rows:
            self._fill_row(row)
        self._sync_step_buttons()

    def _show_steps_hint(self, text: str) -> None:
        self._steps_hint.setText(text)
        self._steps_hint.show()

    def _fill_row(self, row: StepRow) -> None:
        if row.kind == HOTKEY:
            row.set_options(self._hotkeys)
        elif row.kind == EXPRESSION:
            row.set_options(self._expressions)

    def _sync_step_buttons(self) -> None:
        room = len(self.rows) < MAX_STEPS
        self.add_hotkey.setEnabled(room and bool(self._hotkeys))
        self.add_expression.setEnabled(room and bool(self._expressions))
        self.add_wait.setEnabled(room)
        self.add_hotkey.setToolTip("" if self._hotkeys else "Tu modelo no tiene hotkeys o VTube Studio no está conectado")
        self.add_expression.setToolTip(
            "" if self._expressions else "Tu modelo no tiene expresiones o VTube Studio no está conectado"
        )

    def new_step(self, kind: str) -> None:
        self._add_row(Step(kind, seconds=1.0 if kind == WAIT else 0))
        self._sync_step_buttons()

    def _add_row(self, step: Step) -> None:
        row = StepRow(self, step)
        self._fill_row(row)
        self.rows.append(row)
        self._steps.addWidget(row)

    def remove_step(self, row: StepRow) -> None:
        self.rows.remove(row)
        self._steps.removeWidget(row)
        row.deleteLater()
        self._sync_step_buttons()

    def move_step(self, row: StepRow, delta: int) -> None:
        i = self.rows.index(row)
        j = i + delta
        if 0 <= j < len(self.rows):
            self.rows[i], self.rows[j] = self.rows[j], self.rows[i]
            self._steps.removeWidget(row)
            self._steps.insertWidget(j, row)

    def _insert(self, var: str) -> None:
        self.message.insertPlainText(var)
        self.message.setFocus()

    # --------------------------------------------------------------- guardado
    def value(self) -> Reaction:
        kind = self._kind()
        links: tuple[RewardLink, ...] = ()
        if kind is EventKind.REWARD:
            chosen = [picker.selected() for picker in self.pickers.values()]
            links = tuple(RewardLink(r.platform, r.id, r.title) for r in chosen if r)
            platforms = frozenset(link.platform for link in links)
        elif kind is EventKind.RAID:
            platforms = frozenset({Platform.TWITCH})
        else:
            platforms = self.platforms.value()
        return Reaction(
            id=self._reaction.id if self._reaction else None,
            name=self.name.text(),
            trigger=kind,
            steps=tuple(row.value() for row in self.rows),
            message=self.message.toPlainText().replace("\n", " "),
            cooldown_s=self.cooldown.value(),
            platforms=platforms,
            rewards=links,
            min_amount=self.min_amount.value() if kind in MIN_AMOUNTS else 0,
            enabled=self.enabled.isChecked(),
        )

    def _show_error(self, text: str) -> None:
        self.error.setText(_escape(text))
        self.error.show()

    def save(self) -> None:
        data = self.value()
        try:
            if self._reaction:
                self._repo.update(data)
            else:
                self._repo.add(data)
        except ValueError as exc:
            self._show_error(str(exc))
            return
        self.accept()

    def test(self) -> None:
        data = self.value()
        problem = why_cannot_test(self._bot, data)
        if problem:
            self._show_error(problem)
            return
        self.error.hide()
        self._bot.reactions.test(replace(data, name=data.name.strip() or "sin nombre"))

    def _delete(self) -> None:
        answer = QMessageBox.question(self, "Eliminar", f"¿Eliminar la reacción «{self._reaction.name}»?")
        if answer == QMessageBox.Yes:
            self._repo.delete(self._reaction.id)
            self.accept()


class RewardDialog(_AsyncDialog):
    created = Signal(object)  # Reward

    def __init__(self, bot, platform: Platform, parent=None) -> None:
        super().__init__(f"Crear Premio en {platform.label}", "Un premio de puntos de canal gestionado por la app: si "
                         "VTube Studio está cerrado, devuelve los puntos.", "star", parent, width=620)
        self._bot = bot
        self.platform = platform
        self.title_edit = QLineEdit()
        self.title_edit.setMaxLength(MAX_REWARD_TITLE)
        self.title_edit.setPlaceholderText("Saludo del avatar")
        self.title_edit.setMinimumHeight(40)
        self.body.addLayout(vbox(self.field_label("Título", True), self.title_edit, spacing=6))
        self.cost = QSpinBox()
        self.cost.setRange(1, 10_000_000)
        self.cost.setValue(100)
        self.cost.setSuffix(" puntos")
        self.cost.setFixedWidth(180)
        self.cost.setObjectName("mono")
        self.body.addLayout(vbox(self.field_label("Coste", True), hbox(self.cost, None), spacing=6))
        self.prompt = QLineEdit()
        self.prompt.setPlaceholderText("Opcional. Se muestra al canjearlo")
        self.prompt.setMinimumHeight(40)
        self.body.addLayout(vbox(self.field_label("Descripción"), self.prompt, spacing=6))
        self.error = label("", color=t.RED, size=12, wrap=True)
        self.error.hide()
        self.body.addWidget(self.error)
        self.footer.addWidget(button("Cancelar", slot=self.reject))
        self.footer.addStretch(1)
        self._create = button("Crear premio", "check", "primary", lambda: self.spawn(self.submit()))
        self.footer.addWidget(self._create)

    async def submit(self) -> None:
        title = self.title_edit.text().strip()
        if not title:
            self.error.setText("Escribe un título.")
            self.error.show()
            return
        self._create.setEnabled(False)
        try:
            reward = await self._bot.create_reward(self.platform, title, self.cost.value(), self.prompt.text().strip())
        except PlatformError as exc:
            self.error.setText(_escape(str(exc)))
            self.error.show()
            self._create.setEnabled(True)
            return
        self.created.emit(reward)
        self.accept()
