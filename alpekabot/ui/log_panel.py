"""Página «Consola & Logs»: los registros de la app con el estilo del diseño."""

from __future__ import annotations

import html
import logging
import time
from collections import deque

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QTextBrowser, QWidget

from . import theme as t
from .widgets import ChipGroup, button, hbox, label, vbox

MAX_LINES = 3000

_LEVELS = {  # nivel -> (etiqueta, color)
    logging.DEBUG: ("DEBUG", t.TEXT_3),
    logging.INFO: ("INFO", t.BLUE),
    logging.WARNING: ("AVISO", t.AMBER),
    logging.ERROR: ("ERROR", t.RED),
    logging.CRITICAL: ("CRÍTICO", t.RED),
}


class _Emitter(QObject):
    record = Signal(object)  # (hora "HH:MM:SS", nivel, texto)


class QtLogHandler(logging.Handler):
    """Handler de logging que reenvía los registros a la interfaz.

    Es seguro llamarlo desde otros hilos (p. ej. el de EventSub): la señal de
    Qt se encola automáticamente hacia el hilo de la interfaz.
    """

    def __init__(self) -> None:
        super().__init__()
        self.emitter = _Emitter()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
            if record.exc_info:
                text += "\n" + logging.Formatter().formatException(record.exc_info)
            stamp = time.strftime("%H:%M:%S", time.localtime(record.created))
            self.emitter.record.emit((stamp, record.levelno, text))
        except RuntimeError:
            pass  # la ventana ya se cerró
        except Exception:
            self.handleError(record)


class LogPanel(QWidget):
    title = "Consola & Logs"
    icon = "terminal"

    def __init__(self, handler: QtLogHandler, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("page")
        self._records: deque[tuple[str, int, str]] = deque(maxlen=MAX_LINES)
        self._min_level = logging.INFO

        self._levels = ChipGroup([("Todo", logging.INFO, None), ("Avisos", logging.WARNING, "triangle-alert"),
                                  ("Errores", logging.ERROR, "circle-x")], stretch=False)
        self._levels.set_value(logging.INFO)
        self._levels.changed.connect(self._set_level)
        copy = button("Copiar", "copy", slot=self._copy)
        clear = button("Limpiar", "trash-2", slot=self._clear)
        self.actions = QWidget()
        self.actions.setLayout(hbox(self._levels, copy, clear, spacing=10))

        self._view = QTextBrowser()
        self._view.setOpenLinks(False)
        self._view.setStyleSheet(
            f"QTextBrowser {{ background: {t.BG_950}; border: 1px solid {t.BORDER}; border-radius: 12px;"
            f" padding: 12px; font-family: '{t.MONO}'; font-size: 12px; }}"
        )
        self._view.document().setMaximumBlockCount(MAX_LINES)
        head = hbox(label(">_", color=t.ACCENT_SOFT, bold=True, mono=True),
                    label("CONSOLA DEL SISTEMA · LOGS DE EVENTOS", "sectionTitle"), None,
                    label("El log completo se guarda en data/logs/alpekabot.log", color=t.TEXT_3, size=11, mono=True),
                    spacing=8)
        self.setLayout(vbox(head, self._view, spacing=12, margins=(28, 20, 28, 24)))
        handler.emitter.record.connect(self._append)

    # ---------------------------------------------------------------- datos
    @staticmethod
    def _line(stamp: str, level: int, text: str) -> str:
        name, color = _LEVELS.get(level, ("INFO", t.BLUE))
        body_color = {logging.WARNING: t.AMBER, logging.ERROR: "#fca5a5", logging.CRITICAL: "#fca5a5"}.get(level, t.TEXT)
        body = html.escape(text).replace("\n", "<br>&nbsp;&nbsp;&nbsp;&nbsp;")
        return (f'<span style="color:{t.TEXT_3}">{stamp}</span>&nbsp;&nbsp;'
                f'<span style="color:{color}; background-color:{t.alpha(color, 0.15)}; font-weight:600">'
                f'&nbsp;{name}&nbsp;</span>&nbsp;&nbsp;<span style="color:{body_color}">{body}</span>')

    def _append(self, rec: tuple[str, int, str]) -> None:
        self._records.append(rec)
        if rec[1] >= self._min_level:
            bar = self._view.verticalScrollBar()
            at_bottom = bar.value() >= bar.maximum() - 4
            self._view.append(self._line(*rec))
            if at_bottom:
                bar.setValue(bar.maximum())

    def _set_level(self, level: int) -> None:
        self._min_level = level
        self._view.clear()
        for r in self._records:
            if r[1] >= level:
                self._view.append(self._line(*r))
        self._view.verticalScrollBar().setValue(self._view.verticalScrollBar().maximum())

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(
            "\n".join(f"{s} {_LEVELS.get(lv, ('INFO', ''))[0]} {txt}" for s, lv, txt in self._records
                      if lv >= self._min_level))

    def _clear(self) -> None:
        self._records.clear()
        self._view.clear()
