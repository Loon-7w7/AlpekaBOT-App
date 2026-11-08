"""Ventana que anuncia una versión nueva y descarga su instalador."""

from __future__ import annotations

import asyncio
import logging

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QPlainTextEdit, QProgressBar

from ..features.updater import UpdateError, UpdateInfo, Updater
from . import theme as t
from .widgets import ModalDialog, button, label

log = logging.getLogger(__name__)


class UpdateDialog(ModalDialog):
    install_requested = Signal(object, object)  # UpdateInfo, ruta del instalador

    def __init__(self, updater: Updater, info: UpdateInfo, connected: bool, parent=None) -> None:
        super().__init__(f"AlpekaBOT {info.version} disponible", f"Tienes la versión {updater.current}.",
                         "download", parent, width=560)
        self._updater = updater
        self._info = info
        self._task: asyncio.Task | None = None

        # Texto plano: las novedades nunca se interpretan como HTML
        notes = QPlainTextEdit(info.notes.strip() or "Sin notas para esta versión.")
        notes.setReadOnly(True)
        notes.setMaximumHeight(180)
        self.body.addWidget(notes)
        warning = "Se cerrará la app y se volverá a abrir al terminar."
        if connected:
            warning = "Se desconectará del chat y se detendrá la música. " + warning
        self.body.addWidget(label(warning, color=t.AMBER if connected else t.TEXT_2, size=13, wrap=True))

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.hide()
        self.body.addWidget(self._progress)
        self._error = label("", color=t.RED, size=13, wrap=True)
        self._error.hide()
        self.body.addWidget(self._error)

        self._later = button("Más tarde", slot=self.reject)
        self._update = button("Actualizar ahora", "download", "primary", self._on_update)
        self.footer.addWidget(self._later)
        self.footer.addStretch(1)
        self.footer.addWidget(self._update)

    def _on_update(self) -> None:
        self._error.hide()
        self._progress.setValue(0)
        self._progress.show()
        self._update.setEnabled(False)
        self._later.setText("Cancelar")
        self._task = asyncio.ensure_future(self._download())

    def _on_progress(self, received: int, total: int) -> None:
        self._progress.setValue(received * 100 // total)
        self._progress.setFormat(f"{received / 1024 ** 2:.0f} / {total / 1024 ** 2:.0f} MB")

    async def _download(self) -> None:
        try:
            path = await self._updater.download(self._info, self._on_progress)
        except UpdateError as exc:
            self._show_error(str(exc))
        except Exception:
            log.exception("Error descargando la actualización")
            self._show_error("Error inesperado al descargar (mira la consola).")
        else:
            self.accept()
            self.install_requested.emit(self._info, path)

    def _show_error(self, text: str) -> None:
        self._error.setText(text)
        self._error.show()
        self._progress.hide()
        self._update.setEnabled(True)
        self._later.setText("Más tarde")

    def done(self, result: int) -> None:
        if self._task is not None and not self._task.done() and self._task is not asyncio.current_task():
            self._task.cancel()  # Cancelar o cerrar: la descarga a medias se borra
        super().done(result)
