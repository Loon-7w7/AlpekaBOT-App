"""Página «Timers»: tarjetas de mensajes automáticos y modal para crear/editar."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QFrame, QLineEdit, QMessageBox, QPlainTextEdit, QSlider, QSpinBox, QWidget

from ..features.timers import Timer, TimerRepository
from . import theme as t
from .commands_tab import ElidedLabel, VariablesBar, platform_line, scroll_page
from .widgets import (
    StatLine,
    ModalDialog,
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

TIMER_VARIABLES = ["$channel", "$platform", "$uptime", "$game", "$title", "$random(1,100)", "$pick(a|b|c)"]


class TimerRow(QFrame):
    def __init__(self, page: "TimersTab", timer: Timer) -> None:
        super().__init__()
        self.setObjectName("row")
        color = t.TEXT if timer.enabled else t.TEXT_3
        left = QWidget()
        left.setFixedWidth(230)
        left.setLayout(vbox(hbox(icon_label("clock", t.ACCENT_SOFT if timer.enabled else t.TEXT_4, 15),
                                 label(timer.name, size=15, bold=True, color=color), None, spacing=8),
                            platform_line(timer.platforms), spacing=6))
        message = ElidedLabel(timer.message)
        message.setStyleSheet(f"font-size: 14px; color: {color};")
        meta = label(f"Cada <b style='color:{t.TEXT_2}'>{timer.interval_min} min</b> &nbsp;·&nbsp; "
                     f"mínimo {timer.min_lines} mensajes en el chat &nbsp;·&nbsp; solo en directo",
                     color=t.TEXT_3, size=11, mono=True)
        middle = QWidget()
        middle.setLayout(vbox(message, meta, spacing=6))
        toggle = Toggle(timer.enabled)
        toggle.setToolTip("Activar / pausar")
        toggle.toggled.connect(lambda on: page.repo.update(replace(timer, enabled=on)))
        edit = icon_button("pencil", "Editar", lambda: page.open_dialog(timer), t.TEXT_2, 17)
        lay = hbox(left, middle, toggle, 6, edit, spacing=12, margins=(18, 14, 14, 14))
        lay.setStretchFactor(middle, 1)
        self.setLayout(lay)


class TimersTab(QWidget):
    title = "Timers (mensajes automáticos)"
    icon = "clock"

    def __init__(self, repo: TimerRepository, bus, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("page")
        self.repo = repo
        self._timers: list[Timer] = []

        new = button("Nuevo Timer", "plus", "primary", lambda: self.open_dialog(None))
        self.actions = QWidget()
        self.actions.setLayout(hbox(new, spacing=10))

        self._stats = StatLine()
        info = label("Se envían solo en directo, cuando ha pasado el intervalo <b>y</b> ha habido ese número de "
                     "mensajes de otros usuarios desde el último aviso. Como máximo uno por plataforma cada vez.",
                     color=t.TEXT_3, size=12, wrap=True)
        head, self._count = section_header("clock", "Mensajes automáticos")
        self._list = vbox(spacing=10)
        self._empty = label("Todavía no hay timers. Crea uno con «Nuevo Timer».", color=t.TEXT_3, size=13)
        content = vbox(hbox(info, 20, self._stats), VariablesBar(TIMER_VARIABLES), 6, head, self._list,
                       self._empty, None, spacing=14, margins=(28, 20, 28, 24))
        self.setLayout(vbox(scroll_page(content), spacing=0))

        bus.timers_changed.connect(self.reload)
        self.reload()

    @property
    def any_enabled(self) -> bool:
        return any(x.enabled for x in self._timers)

    def reload(self) -> None:
        self._timers = self.repo.list_all()
        active = sum(1 for x in self._timers if x.enabled)
        self._stats.set([(t.GREEN, f"{active} Activos"), (t.AMBER, f"{len(self._timers) - active} Pausados")])
        self._count.setText(f"{len(self._timers)} timers")
        clear_layout(self._list)
        for x in self._timers:
            self._list.addWidget(TimerRow(self, x))
        self._empty.setVisible(not self._timers)

    def open_dialog(self, timer: Timer | None) -> None:
        # open() y no exec(): exec() crea un bucle anidado que frena las tareas asyncio (qasync)
        dlg = TimerDialog(self.repo, timer, self)
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        dlg.open()


class TimerDialog(ModalDialog):
    def __init__(self, repo: TimerRepository, timer: Timer | None, parent=None) -> None:
        super().__init__("Editar Timer" if timer else "Crear Nuevo Timer",
                         "Un mensaje que el bot envía solo cada cierto tiempo mientras estás en directo.",
                         "clock", parent, width=760)
        self._repo = repo
        self._timer = timer
        x = timer or Timer(name="", message="")

        self._name = QLineEdit(x.name)
        self._name.setPlaceholderText("Redes sociales")
        self._name.setMinimumHeight(40)
        self.body.addLayout(vbox(self.field_label("Nombre del Timer", True), self._name, spacing=6))

        self._platforms = PlatformPicker(x.platforms)
        self.body.addLayout(vbox(self.field_label("Plataformas Activas"), self._platforms, spacing=6))

        self._message = QPlainTextEdit(x.message)
        self._message.setObjectName("mono")
        self._message.setPlaceholderText("¡Sígueme en redes! Llevamos $uptime en directo jugando $game")
        self._message.setFixedHeight(90)
        insert = VariablesBar(TIMER_VARIABLES, self._insert, hint="", title="Insertar:")
        self.body.addLayout(vbox(self.field_label("Mensaje", True), insert, self._message, spacing=8))

        self._interval = QSpinBox()
        self._interval.setRange(1, 1440)
        self._interval.setSuffix(" min")
        self._interval.setFixedWidth(100)
        self._interval.setObjectName("mono")
        self._slider = QSlider(Qt.Horizontal)
        self._slider.setRange(1, 120)
        self._slider.valueChanged.connect(self._interval.setValue)
        self._interval.valueChanged.connect(self._sync_slider)
        self._interval.setValue(x.interval_min)
        self._sync_slider(x.interval_min)
        self._min_lines = QSpinBox()
        self._min_lines.setRange(0, 10_000)
        self._min_lines.setSuffix(" mensajes")
        self._min_lines.setFixedWidth(140)
        self._min_lines.setObjectName("mono")
        self._min_lines.setValue(x.min_lines)
        every = vbox(hbox(icon_label("timer", t.AMBER, 15), label("Intervalo", bold=True), None),
                     hbox(self._slider, self._interval, spacing=12),
                     label("Tiempo mínimo entre dos envíos.", color=t.TEXT_3, size=11), spacing=8)
        lines = vbox(hbox(icon_label("message-square", t.BLUE, 15), label("Actividad mínima", bold=True), None),
                     hbox(self._min_lines, None),
                     label("Mensajes de otros usuarios desde el último envío.", color=t.TEXT_3, size=11), spacing=8)
        self.body.addWidget(frame("cardInner", hbox(every, lines, spacing=28, margins=(18, 14, 18, 14))))

        self._enabled = QCheckBox("Timer activo")
        self._enabled.setChecked(x.enabled)
        self.body.addWidget(self._enabled)
        self._error = label("", color=t.RED, size=12, wrap=True)
        self._error.hide()
        self.body.addWidget(self._error)

        if timer:
            self.footer.addWidget(button("Eliminar", "trash-2", "danger", self._delete, icon_color="#fca5a5"))
        self.footer.addWidget(button("Cancelar", slot=self.reject))
        self.footer.addStretch(1)
        self.footer.addWidget(button("Guardar Timer", "check", "primary", self._save))

    def _sync_slider(self, value: int) -> None:
        self._slider.blockSignals(True)
        self._slider.setValue(min(value, 120))
        self._slider.blockSignals(False)

    def _insert(self, var: str) -> None:
        self._message.insertPlainText(var)
        self._message.setFocus()

    def _save(self) -> None:
        data = Timer(
            id=self._timer.id if self._timer else None,
            name=self._name.text(),
            message=self._message.toPlainText().replace("\n", " "),
            interval_min=self._interval.value(),
            min_lines=self._min_lines.value(),
            platforms=self._platforms.value(),
            enabled=self._enabled.isChecked(),
        )
        try:
            if self._timer:
                self._repo.update(data)
            else:
                self._repo.add(data)
        except ValueError as exc:
            self._error.setText(str(exc))
            self._error.show()
            return
        self.accept()

    def _delete(self) -> None:
        if QMessageBox.question(self, "Eliminar", f"¿Eliminar el timer «{self._timer.name}»?") == QMessageBox.Yes:
            self._repo.delete(self._timer.id)
            self.accept()
