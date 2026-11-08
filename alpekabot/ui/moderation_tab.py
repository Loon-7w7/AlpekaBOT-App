"""Pestaña «Moderación»: ajustes de los filtros, palabras prohibidas e historial de acciones."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..core.models import Permission
from ..features.moderation import ModAction, ModerationService, format_escalation, parse_escalation
from . import theme as t
from .widgets import button, fill as _fill, hbox, table as _table

_EXEMPT_LEVELS = (Permission.SUBSCRIBER, Permission.VIP, Permission.MODERATOR)
_GRAY = "#8A8A93"


def _local_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%d/%m %H:%M")
    except ValueError:
        return iso


def _exempt_combo(value: int) -> QComboBox:
    combo = QComboBox()
    for p in _EXEMPT_LEVELS:
        label = f"{p.label} o superior" if p < Permission.MODERATOR else "Solo moderadores y streamer"
        combo.addItem(label, int(p))
    idx = combo.findData(value)
    combo.setCurrentIndex(idx if idx >= 0 else len(_EXEMPT_LEVELS) - 1)
    return combo


class ModerationTab(QWidget):
    title = "Moderación & Filtros"
    icon = "shield"

    def __init__(self, service: ModerationService, bus, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("page")
        self.actions = None
        self._svc = service
        self._words = []
        self._actions: list[ModAction] = []

        tabs = QTabWidget()
        tabs.addTab(self._build_filters(), "Filtros")
        tabs.addTab(self._build_words(), "Palabras prohibidas")
        tabs.addTab(self._build_history(), "Historial")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 20, 28, 20)
        lay.addWidget(tabs)

        bus.banned_words_changed.connect(self._reload_words)
        bus.mod_actions_changed.connect(self._reload_history)
        self._reload_words()
        self._reload_history()

    # --------------------------------------------------------------- filtros
    def _build_filters(self) -> QWidget:
        cfg = self._svc.config

        self._enabled = QCheckBox("Filtros automáticos activados")
        self._enabled.setChecked(cfg.enabled)
        self._announce = QCheckBox("Avisar en el chat de cada sanción automática")
        self._announce.setChecked(cfg.announce)
        self._escalation = QLineEdit(format_escalation(self._svc.steps))
        self._escalation.setToolTip(
            "Qué pasa en la 1.ª, 2.ª, 3.ª… infracción, separado por comas.\n"
            "aviso = borrar el mensaje y avisar · 60, 10m, 1h = timeout · ban = ban permanente.\n"
            "A partir del último paso se repite el último."
        )
        self._escalation.editingFinished.connect(self._save_escalation)
        self._escalation_msg = QLabel(
            f"<span style='color:{_GRAY}'>aviso = borrar + avisar · 60, 10m, 1h = timeout · ban</span>"
        )
        self._reset = QSpinBox()
        self._reset.setRange(1, 10_080)
        self._reset.setSuffix(" min")
        self._reset.setValue(cfg.strike_reset_min)

        general = QGroupBox("General")
        form = QFormLayout(general)
        form.addRow(self._enabled)
        form.addRow(self._announce)
        form.addRow("Escalado:", self._escalation)
        form.addRow("", self._escalation_msg)
        form.addRow("Olvidar infracciones tras:", self._reset)
        note = QLabel(
            f"<span style='color:{_GRAY}'>Las infracciones se cuentan por usuario y plataforma, "
            "sumando todos los filtros. Los moderadores y el streamer nunca se filtran.</span>"
        )
        note.setWordWrap(True)
        form.addRow(note)

        self._links = QCheckBox("Borrar enlaces")
        self._links.setChecked(cfg.links_enabled)
        self._links_exempt = _exempt_combo(cfg.links_exempt)
        self._whitelist = QPlainTextEdit("\n".join(cfg.links_whitelist))
        self._whitelist.setPlaceholderText("Un dominio por línea, p. ej. youtube.com")
        self._whitelist.setMaximumHeight(110)
        save_wl = button("Guardar lista blanca", "check")
        save_wl.clicked.connect(self._save_whitelist)
        self._permit = QSpinBox()
        self._permit.setRange(5, 3600)
        self._permit.setSuffix(" s")
        self._permit.setValue(cfg.permit_s)

        links = QGroupBox("Enlaces")
        lform = QFormLayout(links)
        lform.addRow(self._links)
        lform.addRow("Pueden poner enlaces:", self._links_exempt)
        lform.addRow("Dominios permitidos:", self._whitelist)
        lform.addRow("", save_wl)
        lform.addRow("Duración de !permit:", self._permit)
        lnote = QLabel(
            f"<span style='color:{_GRAY}'>Los subdominios también valen (www.youtube.com, m.youtube.com). "
            "<b>!permit usuario</b> le deja enviar un solo enlace.</span>"
        )
        lnote.setWordWrap(True)
        lform.addRow(lnote)

        self._words_on = QCheckBox("Borrar mensajes con palabras prohibidas")
        self._words_on.setChecked(cfg.words_enabled)
        self._words_exempt = _exempt_combo(cfg.words_exempt)
        words = QGroupBox("Palabras prohibidas")
        wform = QFormLayout(words)
        wform.addRow(self._words_on)
        wform.addRow("Exentos:", self._words_exempt)

        for chk in (self._enabled, self._announce, self._links, self._words_on):
            chk.toggled.connect(self._save)
        for spin in (self._reset, self._permit):
            spin.valueChanged.connect(self._save)
        for combo in (self._links_exempt, self._words_exempt):
            combo.currentIndexChanged.connect(self._save)

        commands = QLabel(
            f"<span style='color:{_GRAY}'>Comandos para mods (Twitch y Kick): "
            "<b>!timeout usuario [600 | 10m | 1h] [motivo]</b>, <b>!ban usuario [motivo]</b>, "
            "<b>!unban usuario</b>, <b>!permit usuario</b>. También puedes hacer clic derecho "
            "sobre el nombre de alguien en el chat.</span>"
        )
        commands.setWordWrap(True)

        inner = QWidget()
        lay = QVBoxLayout(inner)
        for w in (general, links, words, commands):
            lay.addWidget(w)
        lay.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        return scroll

    def _save(self) -> None:
        cfg = self._svc.config
        cfg.enabled = self._enabled.isChecked()
        cfg.announce = self._announce.isChecked()
        cfg.strike_reset_min = self._reset.value()
        cfg.links_enabled = self._links.isChecked()
        cfg.links_exempt = self._links_exempt.currentData()
        cfg.permit_s = self._permit.value()
        cfg.words_enabled = self._words_on.isChecked()
        cfg.words_exempt = self._words_exempt.currentData()
        self._svc.save()

    def _save_escalation(self) -> None:
        try:
            steps = parse_escalation(self._escalation.text())
        except ValueError as exc:
            self._escalation_msg.setText(f"<span style='color:#F85149'>{exc}</span>")
            return
        self._svc.config.escalation = steps
        self._svc.save()
        self._escalation.setText(format_escalation(steps))
        self._escalation_msg.setText("<span style='color:#3FB950'>Guardado</span>")

    def _save_whitelist(self) -> None:
        domains = []
        for line in self._whitelist.toPlainText().splitlines():
            d = line.strip().lower()
            if d and d not in domains:
                domains.append(d)
        self._svc.config.links_whitelist = domains
        self._svc.save()
        self._whitelist.setPlainText("\n".join(domains))

    # ------------------------------------------------------------- palabras
    def _build_words(self) -> QWidget:
        self._word_input = QLineEdit()
        self._word_input.setPlaceholderText("Palabra o frase prohibida")
        self._word_input.returnPressed.connect(self._add_word)
        self._word_regex = QCheckBox("Expresión regular")
        add = button("Añadir", "plus", "primary")
        add.clicked.connect(self._add_word)
        row = QHBoxLayout()
        row.addWidget(self._word_input, 1)
        row.addWidget(self._word_regex)
        row.addWidget(add)
        self._word_msg = QLabel()
        self._word_table = _table(["Palabra", "Tipo"], 0)
        info = QLabel(
            f"<span style='color:{_GRAY}'>Se busca la palabra completa (y su plural) sin distinguir "
            "mayúsculas ni acentos, y detecta trucos como «puuuta», «p.u.t.a» o «pvt4»; "
            "«computadora» no salta. Las expresiones regulares se aplican al texto en minúsculas "
            "y sin acentos.</span>"
        )
        info.setWordWrap(True)
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addLayout(row)
        lay.addWidget(self._word_msg)
        lay.addWidget(self._word_table, 1)
        lay.addLayout(hbox(button("Quitar", "trash-2", slot=self._remove_word), None))
        lay.addWidget(info)
        return w

    def _add_word(self) -> None:
        try:
            self._svc.repo.word_add(self._word_input.text(), self._word_regex.isChecked())
        except ValueError as exc:
            self._word_msg.setText(f"<span style='color:#F85149'>{exc}</span>")
            return
        self._word_input.clear()
        self._word_msg.clear()

    def _remove_word(self) -> None:
        r = self._word_table.currentRow()
        if 0 <= r < len(self._words):
            self._svc.repo.word_remove(self._words[r].id)

    def _reload_words(self) -> None:
        self._words = self._svc.repo.words_list()
        _fill(self._word_table, [[w.pattern, "Regex" if w.is_regex else "Palabra"] for w in self._words])

    # ------------------------------------------------------------ historial
    def _build_history(self) -> QWidget:
        self._history = _table(["Fecha", "Plataforma", "Usuario", "Acción", "Motivo", "Por", "Mensaje"], 6)
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addLayout(hbox(button("Borrar historial", "trash-2", "danger", self._clear_history, icon_color="#fca5a5"), None))
        lay.addWidget(self._history, 1)
        lay.addWidget(QLabel(f"<span style='color:{_GRAY}'>Se muestran las 500 acciones más recientes.</span>"))
        return w

    def _reload_history(self) -> None:
        self._actions = self._svc.repo.recent_actions()
        _fill(self._history, [
            [_local_time(a.created_at), a.platform.label, a.user_name, a.label, a.reason,
             f"{a.moderator} (auto)" if a.source == "auto" else a.moderator, a.message]
            for a in self._actions
        ])

    def _clear_history(self) -> None:
        answer = QMessageBox.question(self, "Borrar historial", "¿Borrar todo el historial de moderación?")
        if answer == QMessageBox.Yes:
            self._svc.repo.clear_actions()
