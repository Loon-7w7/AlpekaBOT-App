"""Arranque de la aplicación: logging, Qt y el bucle asyncio integrado (qasync)."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from logging.handlers import RotatingFileHandler

import qasync
from PySide6.QtCore import QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from . import APP_NAME, __version__
from .bot import AlpekaBot
from .config import AppConfig
from .paths import LOG_DIR, ensure_dirs
from .ui.log_panel import QtLogHandler
from .ui.main_window import MainWindow
from .ui.theme import ASSETS, apply_dark_theme

log = logging.getLogger(__name__)

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def _setup_logging() -> QtLogHandler:
    ensure_dirs()
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    file_handler = RotatingFileHandler(
        LOG_DIR / "alpekabot.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(file_handler)

    # Con pythonw no hay consola (sys.stderr es None)
    if sys.stderr is not None:
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):  # consola de Windows (cp1252) -> UTF-8
                stream.reconfigure(encoding="utf-8", errors="replace")
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        console.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(console)

    ui_handler = QtLogHandler()
    ui_handler.setLevel(logging.INFO)
    ui_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s  %(message)s", "%H:%M:%S"))
    root.addHandler(ui_handler)

    # Librerías muy habladoras: solo avisos en adelante
    for noisy in ("twitchAPI", "aiohttp", "asyncio", "qasync"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    def _excepthook(exc_type, exc, tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):  # Ctrl+C: no es un error
            _request_close()
            return
        log.critical("Excepción no controlada", exc_info=(exc_type, exc, tb))

    sys.excepthook = _excepthook
    return ui_handler


_window: MainWindow | None = None
_close_requested = False


def _request_close() -> None:
    """Cierra la ventana con el cierre ordenado (desconecta plataformas, borra suscripciones...).

    Un segundo Ctrl+C fuerza la salida por si el cierre ordenado se queda colgado.
    """
    global _close_requested
    if _window is None or _close_requested:
        log.warning("Saliendo sin el cierre ordenado")
        QApplication.quit()
        return
    _close_requested = True
    log.info("Ctrl+C recibido: cerrando AlpekaBOT… (pulsa Ctrl+C otra vez para forzar)")
    QTimer.singleShot(0, _window.close)


def _install_sigint_handler() -> QTimer:
    """Ctrl+C en la consola cierra la app de forma ordenada en vez de lanzar KeyboardInterrupt.

    Python solo atiende las señales cuando ejecuta código Python; el temporizador
    le da esa oportunidad aunque Qt esté esperando eventos.
    """
    signal.signal(signal.SIGINT, lambda *_: _request_close())
    wakeup = QTimer()
    wakeup.timeout.connect(lambda: None)
    wakeup.start(250)
    return wakeup


def run() -> int:
    ui_handler = _setup_logging()
    log.info("Iniciando %s %s", APP_NAME, __version__)

    # El reproductor de canciones debe poder sonar sin que hagas clic en la página
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--autoplay-policy=no-user-gesture-required")
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(QIcon(str(ASSETS / "app.ico")))
    apply_dark_theme(app)

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    bot = AlpekaBot(AppConfig.load())
    window = MainWindow(bot, ui_handler)
    global _window
    _window = window
    sigint_wakeup = _install_sigint_handler()  # noqa: F841 (debe seguir vivo)
    window.show()
    QTimer.singleShot(0, window.on_startup)

    closed = asyncio.Event()
    app.aboutToQuit.connect(closed.set)
    with loop:
        loop.run_until_complete(closed.wait())
    log.info("AlpekaBOT cerrado")
    return 0
