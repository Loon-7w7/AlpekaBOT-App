"""Página «Comandos»: filtros, variables, tarjetas de comandos y modal para crear/editar."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QScrollArea,
    QSlider,
    QSpinBox,
    QWidget,
)

from ..core.models import Permission, Platform
from ..features.commands import MAX_REPLY_LEN, Command, CommandRepository
from . import theme as t
from .widgets import (
    StatLine,
    ChipGroup,
    Dot,
    FlowLayout,
    ModalDialog,
    Pill,
    PlatformPicker,
    SearchBox,
    Toggle,
    button,
    clear_layout,
    frame,
    hbox,
    icon_button,
    icon_label,
    label,
    platforms_text,
    section_header,
    soon,
    vbox,
)

VARIABLES = ["$user", "$target", "$args", "$count", "$channel", "$uptime", "$game", "$title",
             "$platform", "$random(1,100)", "$pick(a|b|c)", "$readapi(https://…)"]

PERMISSION_STYLE = {  # permiso -> (texto corto, icono, color)
    Permission.EVERYONE: ("Todos", "users", t.TEXT_2),
    Permission.SUBSCRIBER: ("Subs", "star", t.ACCENT_SOFT),
    Permission.VIP: ("VIP", "gem", t.PINK),
    Permission.MODERATOR: ("MOD", "shield", t.BLUE),
    Permission.BROADCASTER: ("Streamer", "crown", t.GOLD),
}

# Comandos internos: nombre -> (argumentos, descripción, etiqueta)
BUILTIN_INFO = {
    "sr": ("[enlace/texto]", "Agrega una canción de YouTube a la cola de reproducción.", "SongRequest"),
    "song": ("", "Dice qué canción suena y quién la pidió.", "SongRequest"),
    "queue": ("", "Muestra las próximas canciones de la cola.", "SongRequest"),
    "wrongsong": ("", "Quita tu última petición pendiente de la cola.", "SongRequest"),
    "skip": ("", "Salta la canción actual.", "SongRequest"),
    "pause": ("", "Pausa la música.", "SongRequest"),
    "play": ("", "Reanuda la música.", "SongRequest"),
    "volume": ("[0-100]", "Consulta o cambia el volumen del reproductor.", "SongRequest"),
    "clearqueue": ("", "Vacía la cola de peticiones.", "SongRequest"),
    "addcmd": ("!nombre respuesta", "Crea un comando desde el chat.", "Gestión"),
    "editcmd": ("!nombre respuesta", "Cambia la respuesta de un comando.", "Gestión"),
    "delcmd": ("!nombre", "Elimina un comando.", "Gestión"),
    "timeout": ("usuario [10m] [motivo]", "Silencia a un usuario un tiempo (600 s por defecto).", "Moderación"),
    "ban": ("usuario [motivo]", "Banea a un usuario de forma permanente.", "Moderación"),
    "unban": ("usuario", "Quita el ban o el timeout de un usuario.", "Moderación"),
    "permit": ("usuario", "Deja a un usuario enviar un enlace durante un minuto.", "Moderación"),
    "skiptts": ("", "Calla el mensaje del TTS que está sonando.", "TTS"),
}
_TAG_COLORS = {"SongRequest": t.AMBER, "Gestión": t.ACCENT_SOFT, "Moderación": t.ORANGE, "TTS": t.GREEN}


def permission_pill(p: Permission) -> Pill:
    text, _icon, color = PERMISSION_STYLE[p]
    return Pill(text, color, bg=0.08, border=0.35, size=11, bold=False)


def scroll_page(inner_layout) -> QScrollArea:
    inner = QWidget()
    inner.setLayout(inner_layout)
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.NoFrame)
    area.setWidget(inner)
    return area


def platform_line(platforms: frozenset[Platform]) -> QWidget:
    text, color = platforms_text(platforms)
    w = QWidget()
    w.setLayout(hbox(Dot(color, 6), label(text, color=color, size=11, mono=True), None, spacing=6))
    return w


def _copy(text: str) -> None:
    QGuiApplication.clipboard().setText(text)


class VariablesBar(QFrame):
    """«</> VARIABLES: $user $target …» — clic para copiar (o insertar en un editor)."""

    def __init__(self, variables: list[str], on_click=None, hint: str = "Clic en una variable para copiarla",
                 title: str = "&lt;/&gt; VARIABLES:") -> None:
        super().__init__()
        self.setObjectName("card")
        flow = FlowLayout(8)
        for v in variables:
            b = button(v, object_name="varChip")
            b.clicked.connect(lambda _=False, var=v: (on_click or _copy)(var))
            flow.addWidget(b)
        chips = QWidget()
        chips.setLayout(flow)
        head = label(title, size=12, bold=True, mono=True, color=t.ACCENT_SOFT)
        row = hbox(head, chips, spacing=12, margins=(16, 10, 16, 10))
        row.setStretchFactor(chips, 1)
        row.setAlignment(head, Qt.AlignTop)
        if hint:
            h = label(hint, color=t.TEXT_3, size=11, mono=True)
            h.setStyleSheet(h.styleSheet() + " font-style: italic;")
            row.addWidget(h, 0, Qt.AlignTop)
        self.setLayout(row)


class ElidedLabel(QLabel):
    """Etiqueta de una línea que recorta con «…» según el ancho disponible."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self._full = text
        self.setToolTip(text)
        self.setMinimumWidth(80)

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt)
        super().resizeEvent(event)
        self.setText(self.fontMetrics().elidedText(self._full, Qt.ElideRight, max(40, self.width())))


# ======================================================================= filas
class CommandRow(QFrame):
    def __init__(self, page: "CommandsTab", cmd: Command) -> None:
        super().__init__()
        self.setObjectName("row")
        color = t.TEXT if cmd.enabled else t.TEXT_3
        left = QWidget()
        left.setFixedWidth(230)
        left.setLayout(vbox(label(f"!{cmd.name}", size=16, bold=True, mono=True, color=color),
                            platform_line(cmd.platforms), spacing=6))

        response = ElidedLabel(cmd.response)
        response.setStyleSheet(f"font-size: 14px; color: {color};")
        uses = f"{cmd.use_count:,}".replace(",", ".")
        meta = label(f"Cooldown: <b style='color:{t.TEXT_2}'>{cmd.cooldown_s}s</b> &nbsp;·&nbsp; {uses} ejecuciones",
                     color=t.TEXT_3, size=11, mono=True)
        middle = QWidget()
        middle.setLayout(vbox(response, meta, spacing=6))

        toggle = Toggle(cmd.enabled)
        toggle.setToolTip("Activar / pausar")
        toggle.toggled.connect(lambda on: page.repo.update(replace(cmd, enabled=on)))
        edit = icon_button("pencil", "Editar", lambda: page.open_dialog(cmd), t.TEXT_2, 17)
        test = soon(icon_button("play", "", None, t.TEXT_2, 17), "probar el comando")

        lay = hbox(left, middle, permission_pill(cmd.permission), 10, toggle, 6, edit, test,
                   spacing=12, margins=(18, 14, 14, 14))
        lay.setStretchFactor(middle, 1)
        self.setLayout(lay)


class BuiltinRow(QFrame):
    def __init__(self, name: str, permission: Permission, info: dict | None = None) -> None:
        super().__init__()
        self.setObjectName("row")
        args, desc, tag = {**BUILTIN_INFO, **(info or {})}.get(name, ("", "Comando interno.", "Sistema"))
        title = hbox(label(f"!{name}", size=16, bold=True, mono=True), spacing=8)
        if args:
            title.addWidget(label(args, color=t.TEXT_3, size=11, mono=True))
        title.addWidget(Pill(tag, _TAG_COLORS.get(tag, t.TEXT_2), size=10, bold=False))
        title.addStretch(1)
        left = QWidget()
        left.setFixedWidth(360)
        left.setLayout(vbox(title, platform_line(frozenset(Platform)), spacing=6))
        meta = label("Integrado en AlpekaBOT · no se puede editar", color=t.TEXT_3, size=11, mono=True)
        middle = QWidget()
        middle.setLayout(vbox(label(desc, size=14), meta, spacing=6))
        lay = hbox(left, middle, permission_pill(permission), 10, soon(Toggle(True), "pausar comandos internos"),
                   spacing=12, margins=(18, 14, 18, 14))
        lay.setStretchFactor(middle, 1)
        self.setLayout(lay)


# ====================================================================== página
class CommandsTab(QWidget):
    title = "Comandos (!commands)"
    icon = "terminal"

    def __init__(self, repo: CommandRepository, bus, builtins=None, parent=None, builtin_info=None) -> None:
        super().__init__(parent)
        self.setObjectName("page")
        self.repo = repo
        self._builtins = builtins or (lambda: [])
        self._builtin_info = builtin_info or (lambda: {})  # descripción de comandos con nombre configurable
        self._commands: list[Command] = []

        # ---- acciones de la cabecera
        self._search = SearchBox("Buscar comando…")
        self._search.setFixedWidth(230)
        self._search.textChanged.connect(self.reload)
        io = soon(icon_button("arrow-up-down", "", None, t.TEXT_2, 17, "iconBox"), "importar / exportar comandos")
        new = button("Nuevo Comando", "plus", "primary", lambda: self.open_dialog(None))
        self.actions = QWidget()
        self.actions.setLayout(hbox(self._search, io, new, spacing=10))

        # ---- filtros
        self._kind = ChipGroup([("Todos", "all", None), ("Personalizados", "custom", None),
                                ("Predeterminados", "builtin", None)], stretch=False)
        self._kind.set_value("all")
        self._kind.changed.connect(lambda _d: self.reload())
        self._tw = button("Twitch", object_name="chip")
        self._kk = button("Kick", object_name="chip")
        for b, color in ((self._tw, t.ACCENT), (self._kk, t.KICK)):
            b.setCheckable(True)
            b.setChecked(True)
            b.setIcon(t.icon("radio", color, 12))
            b.toggled.connect(lambda _on: self.reload())
        sep = QFrame()
        sep.setFixedSize(1, 22)
        sep.setStyleSheet(f"background: {t.BORDER};")
        self._stats = StatLine()
        filters = hbox(self._kind, sep, self._tw, self._kk, None, self._stats, spacing=10)

        # ---- listas
        custom_head, self._custom_count = section_header("star", "Comandos personalizados & comunidad")
        self._custom_list = vbox(spacing=10)
        builtin_head, self._builtin_count = section_header(
            "monitor", "Comandos del sistema / reproductor / moderación", color=t.BLUE)
        self._builtin_list = vbox(spacing=10)
        self._empty = label("No hay comandos que coincidan. Crea uno con «Nuevo Comando» o desde el chat con "
                            "!addcmd !nombre respuesta.", color=t.TEXT_3, size=13, wrap=True)
        self._custom_box = QWidget()
        self._custom_box.setLayout(vbox(custom_head, self._custom_list, self._empty, spacing=10))
        self._builtin_box = QWidget()
        self._builtin_box.setLayout(vbox(builtin_head, self._builtin_list, spacing=10))

        content = vbox(filters, VariablesBar(VARIABLES), 6, self._custom_box, 10, self._builtin_box, None,
                       spacing=14, margins=(28, 20, 28, 24))
        self.setLayout(vbox(scroll_page(content), spacing=0))

        bus.commands_changed.connect(self.reload)
        self.reload()

    # ------------------------------------------------------------------ datos
    def _platform_filter(self) -> set[Platform]:
        sel = set()
        if self._tw.isChecked():
            sel.add(Platform.TWITCH)
        if self._kk.isChecked():
            sel.add(Platform.KICK)
        return sel

    def reload(self) -> None:
        self._commands = self.repo.list_all()
        builtins = sorted(self._builtins(), key=lambda b: (b[1], b[0]))
        query = self._search.text().strip().lower().lstrip("!")
        plats = self._platform_filter()
        kind = self._kind.value()

        custom = [c for c in self._commands
                  if (not query or query in c.name.lower() or query in c.response.lower()) and (c.platforms & plats)]
        system = [b for b in builtins if (not query or query in b[0]) and plats]

        active = sum(1 for c in self._commands if c.enabled)
        paused = len(self._commands) - active
        self._kind.set_text("all", f"Todos  {len(self._commands) + len(builtins)}")
        self._kind.set_text("custom", f"Personalizados  {len(self._commands)}")
        self._kind.set_text("builtin", f"Predeterminados  {len(builtins)}")
        self._stats.set([(t.GREEN, f"{active} Activos"), (t.AMBER, f"{paused} Pausados")])
        self._custom_count.setText(f"{len(custom)} comandos")
        self._builtin_count.setText(f"{len(system)} comandos")

        clear_layout(self._custom_list)
        for c in custom:
            self._custom_list.addWidget(CommandRow(self, c))
        clear_layout(self._builtin_list)
        for name, perm in system:
            self._builtin_list.addWidget(BuiltinRow(name, perm, self._builtin_info()))
        self._empty.setVisible(not custom)
        self._custom_box.setVisible(kind in ("all", "custom"))
        self._builtin_box.setVisible(kind in ("all", "builtin"))

    @property
    def count(self) -> int:
        return len(self._commands)

    def open_dialog(self, cmd: Command | None) -> None:
        # open() y no exec(): exec() crea un bucle anidado que frena las tareas asyncio (qasync)
        dlg = CommandDialog(self.repo, cmd, self)
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        dlg.open()


# ======================================================================= modal
class PrefixedLine(QLineEdit):
    """Campo con un prefijo fijo dibujado dentro («!»)."""

    def __init__(self, prefix: str, text: str = "") -> None:
        super().__init__(text)
        self.setObjectName("mono")
        self._prefix = QLabel(prefix, self)
        self._prefix.setStyleSheet(f"color: {t.ACCENT_SOFT}; font-family: '{t.MONO}'; font-weight: 700;")
        self._prefix.move(14, 11)
        self.setTextMargins(16, 0, 0, 0)
        self.setMinimumHeight(40)


class CommandDialog(ModalDialog):
    def __init__(self, repo: CommandRepository, cmd: Command | None, parent=None) -> None:
        super().__init__(
            "Editar Comando" if cmd else "Crear Nuevo Comando",
            "Configura la respuesta, los permisos, las plataformas y el cooldown.",
            "terminal", parent, width=820,
        )
        self._repo = repo
        self._cmd = cmd
        cmd = cmd or Command(name="", response="")

        # nombre + alias
        self._name = PrefixedLine("!", cmd.name)
        self._name.setPlaceholderText("sorteo")
        alias = QLineEdit()
        alias.setObjectName("mono")
        alias.setPlaceholderText("giveaway, rifa")
        alias.setMinimumHeight(40)
        soon(alias, "alias de comandos")
        name_col = vbox(self.field_label("Nombre del Comando", True, "sin '!' ni espacios"), self._name, spacing=6)
        alias_col = vbox(self.field_label("Alias secundarios", hint="próximamente"), alias, spacing=6)
        self.body.addLayout(hbox(name_col, alias_col, spacing=16))

        # plataformas
        self._platforms = PlatformPicker(cmd.platforms)
        self.body.addLayout(vbox(self.field_label("Plataformas Activas"), self._platforms, spacing=6))

        # permisos
        self._perm = ChipGroup([
            ("Todos (Público)", Permission.EVERYONE, "users"),
            ("Suscriptores", Permission.SUBSCRIBER, "star"),
            ("VIP", Permission.VIP, "gem"),
            ("Moderadores", Permission.MODERATOR, "shield"),
            ("Solo Streamer", Permission.BROADCASTER, "crown"),
        ])
        self._perm.set_value(cmd.permission)
        self.body.addLayout(vbox(self.field_label("¿Quién puede usar este comando?"), self._perm, spacing=6))

        # respuesta
        self._counter = label("", color=t.TEXT_3, size=11, mono=True)
        resp_head = hbox(QLabel(f"<b>Mensaje / Respuesta del Bot</b> <span style='color:{t.ACCENT}'>*</span>"),
                         None, self._counter)
        self._response = QPlainTextEdit(cmd.response)
        self._response.setObjectName("mono")
        self._response.setPlaceholderText("¡$user ha entrado al sorteo! Buena suerte 🎉")
        self._response.setFixedHeight(96)
        self._response.textChanged.connect(self._update_counter)
        insert = VariablesBar(VARIABLES[:9], self._insert, hint="", title="Insertar:")
        self.body.addLayout(vbox(resp_head, insert, self._response, spacing=8))

        # cooldowns
        self._cd_slider = QSlider(Qt.Horizontal)
        self._cd_slider.setRange(0, 300)
        self._cd_spin = QSpinBox()
        self._cd_spin.setRange(0, 86_400)
        self._cd_spin.setSuffix("s")
        self._cd_spin.setFixedWidth(84)
        self._cd_spin.setObjectName("mono")
        self._cd_value = label("", color=t.TEXT_3, size=11, mono=True)
        self._cd_slider.valueChanged.connect(self._cd_spin.setValue)
        self._cd_spin.valueChanged.connect(self._on_cooldown)
        self._cd_spin.setValue(cmd.cooldown_s)
        self._on_cooldown(cmd.cooldown_s)
        glob = vbox(hbox(icon_label("clock", t.AMBER), label("Cooldown Global", bold=True), None, self._cd_value),
                    hbox(self._cd_slider, self._cd_spin, spacing=12),
                    label("Tiempo mínimo entre usos (vale para todos, mods incluidos).", color=t.TEXT_3, size=11),
                    spacing=8)
        user_slider = soon(QSlider(Qt.Horizontal), "cooldown por usuario")
        user_spin = soon(QSpinBox(), "cooldown por usuario")
        user_spin.setFixedWidth(84)
        per_user = vbox(hbox(icon_label("user", t.TEXT_4), label("Cooldown por Usuario", bold=True, color=t.TEXT_3),
                             None, label("próximamente", color=t.TEXT_4, size=11, mono=True)),
                        hbox(user_slider, user_spin, spacing=12),
                        label("Evita que un mismo usuario haga spam.", color=t.TEXT_4, size=11), spacing=8)
        self.body.addWidget(frame("cardInner", hbox(glob, per_user, spacing=28, margins=(18, 14, 18, 14))))

        # opciones
        self._enabled = QCheckBox("Comando activo")
        self._enabled.setChecked(cmd.enabled)
        whisper = soon(QCheckBox("Solo susurro / DM al usuario"), "respuestas por susurro")
        self.body.addLayout(hbox(self._enabled, 16, whisper, None))

        self._error = label("", color=t.RED, size=12, wrap=True)
        self._error.hide()
        self.body.addWidget(self._error)

        # pie
        if self._cmd:
            self.footer.addWidget(button("Eliminar", "trash-2", "danger", self._delete, icon_color="#fca5a5"))
        self.footer.addWidget(button("Cancelar", slot=self.reject))
        self.footer.addStretch(1)
        self.footer.addWidget(soon(button("Probar Comando", "play", "outlineAccent", icon_color=t.ACCENT_SOFT),
                                   "probar el comando"))
        self.footer.addWidget(button("Guardar Comando", "check", "primary", self._save))
        self._update_counter()

    def _on_cooldown(self, value: int) -> None:
        self._cd_slider.blockSignals(True)
        self._cd_slider.setValue(min(value, 300))
        self._cd_slider.blockSignals(False)
        self._cd_value.setText(f"{value} seg")

    def _insert(self, var: str) -> None:
        self._response.insertPlainText(var)
        self._response.setFocus()

    def _update_counter(self) -> None:
        n = len(self._response.toPlainText())
        color = t.RED if n > MAX_REPLY_LEN else t.TEXT_3
        self._counter.setText(f"<span style='color:{color}'>{n} / {MAX_REPLY_LEN} car.</span>")

    def _save(self) -> None:
        data = Command(
            id=self._cmd.id if self._cmd else None,
            name=self._name.text(),
            response=self._response.toPlainText().replace("\n", " "),
            permission=self._perm.value() if self._perm.value() is not None else Permission.EVERYONE,
            cooldown_s=self._cd_spin.value(),
            platforms=self._platforms.value(),
            enabled=self._enabled.isChecked(),
            use_count=self._cmd.use_count if self._cmd else 0,
        )
        try:
            if self._cmd:
                self._repo.update(data)
            else:
                self._repo.add(data)
        except ValueError as exc:
            self._error.setText(str(exc))
            self._error.show()
            return
        self.accept()

    def _delete(self) -> None:
        answer = QMessageBox.question(self, "Eliminar", f"¿Eliminar el comando !{self._cmd.name}?")
        if answer == QMessageBox.Yes:
            self._repo.delete(self._cmd.id)
            self.accept()

