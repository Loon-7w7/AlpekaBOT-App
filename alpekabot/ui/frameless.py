"""Ventana sin marco que sigue comportándose como una ventana de Windows.

Se quita el marco con ``Qt.FramelessWindowHint`` pero se vuelven a poner los
estilos nativos (``WS_THICKFRAME``, ``WS_CAPTION``…) y se atienden dos mensajes:

- ``WM_NCCALCSIZE``: el área de cliente ocupa toda la ventana (sin barra de título).
- ``WM_NCHITTEST``: los bordes redimensionan y la cabecera de la app arrastra la
  ventana (``HTCAPTION``). Así funcionan el encaje (Win+flechas, arrastrar al
  borde), el doble clic para maximizar, la sombra y las esquinas redondeadas
  de Windows 11, sin reprogramarlos.

Fuera de Windows se usa ``startSystemMove`` como alternativa.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from typing import Callable

from PySide6.QtCore import QEvent, QPoint, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QCursor, QPainter
from PySide6.QtWidgets import QAbstractButton, QComboBox, QLineEdit, QWidget

from . import theme as t
from .widgets import hbox, icon_button

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084
HTCAPTION = 2
HTLEFT, HTRIGHT, HTTOP, HTTOPLEFT, HTTOPRIGHT, HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT = 10, 11, 12, 13, 14, 15, 16, 17
GWL_STYLE = -16
WS_MINIMIZEBOX, WS_MAXIMIZEBOX, WS_THICKFRAME, WS_SYSMENU, WS_CAPTION = 0x20000, 0x10000, 0x40000, 0x80000, 0xC00000
SWP_FLAGS = 0x0002 | 0x0001 | 0x0004 | 0x0020  # NOMOVE | NOSIZE | NOZORDER | FRAMECHANGED
SM_CXSIZEFRAME, SM_CXPADDEDBORDER = 32, 92
DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_DONOTROUND = 33, 1
DWMWA_BORDER_COLOR, DWMWA_COLOR_NONE = 34, 0xFFFFFFFE
CORNER_RADIUS = 14  # radio de las esquinas de la ventana (las dibuja la app)

_INTERACTIVE = (QAbstractButton, QLineEdit, QComboBox)


if IS_WINDOWS:
    from ctypes import wintypes

    class _MARGINS(ctypes.Structure):
        _fields_ = [("left", ctypes.c_int), ("right", ctypes.c_int), ("top", ctypes.c_int), ("bottom", ctypes.c_int)]

    class _NCCALCSIZE_PARAMS(ctypes.Structure):
        _fields_ = [("rgrc", wintypes.RECT * 3), ("lppos", ctypes.c_void_p)]

    _user32 = ctypes.windll.user32
    _dwm = ctypes.windll.dwmapi
    _user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    _user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    _user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    _user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    _user32.IsZoomed.argtypes = [wintypes.HWND]
    _user32.GetDpiForWindow.argtypes = [wintypes.HWND]
    _dwm.DwmExtendFrameIntoClientArea.argtypes = [wintypes.HWND, ctypes.c_void_p]
    _dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]


def is_caption_area(container: QWidget, window: QWidget, local: QPoint) -> bool:
    """¿El punto (en coordenadas de la ventana) cae en una zona libre de ``container``?"""
    if not container.isVisible():
        return False
    p = container.mapFrom(window, local)
    if not container.rect().contains(p):
        return False
    child = container.childAt(p)
    while child is not None and child is not container:
        if isinstance(child, _INTERACTIVE):
            return False
        child = child.parentWidget()
    return True


class FramelessHelper:
    """Hay que crearlo ANTES de mostrar la ventana y llamar a ``native_event`` desde ``nativeEvent``."""

    def __init__(self, window: QWidget, is_caption: Callable[[QPoint], bool], border: int = 6) -> None:
        self._win = window
        self._is_caption = is_caption
        self._border = border
        self._native_ok = False
        window.setWindowFlags(window.windowFlags() | Qt.FramelessWindowHint | Qt.Window)

    def after_show(self) -> None:
        """Restaura los estilos nativos (redimensionar, encajar, sombra, animaciones)."""
        if not IS_WINDOWS or self._native_ok:
            return
        try:
            hwnd = int(self._win.winId())
            style = _user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
            _user32.SetWindowLongPtrW(
                hwnd, GWL_STYLE, style | WS_THICKFRAME | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX | WS_MAXIMIZEBOX
            )
            margins = _MARGINS(1, 1, 1, 1)  # deja que DWM dibuje la sombra
            _dwm.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins))
            # Las esquinas las dibuja la app (radio mayor que el de Windows 11): se desactivan las
            # de Windows y su borde fino, que si no se verían por fuera de las nuestras.
            # En Windows 10 estos atributos no existen y DWM devuelve un error que se ignora.
            corner = ctypes.c_int(DWMWCP_DONOTROUND)
            _dwm.DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(corner),
                                       ctypes.sizeof(corner))
            no_border = ctypes.c_uint(DWMWA_COLOR_NONE)
            _dwm.DwmSetWindowAttribute(hwnd, DWMWA_BORDER_COLOR, ctypes.byref(no_border), ctypes.sizeof(no_border))
            self._native_ok = True
            _user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, SWP_FLAGS)
        except Exception:
            self._native_ok = False
            log.warning("No se pudo preparar la ventana sin marco nativa", exc_info=True)

    # ------------------------------------------------------------ mensajes
    def native_event(self, event_type, message) -> tuple[bool, int] | None:
        if not (IS_WINDOWS and self._native_ok) or bytes(event_type) != b"windows_generic_MSG":
            return None
        msg = wintypes.MSG.from_address(int(message))
        if msg.message == WM_NCCALCSIZE and msg.wParam:
            if _user32.IsZoomed(msg.hWnd):
                # Maximizada, Windows la agranda por el borde invisible: se compensa
                dpi = _user32.GetDpiForWindow(msg.hWnd)
                thick = (_user32.GetSystemMetricsForDpi(SM_CXSIZEFRAME, dpi)
                         + _user32.GetSystemMetricsForDpi(SM_CXPADDEDBORDER, dpi))
                r = _NCCALCSIZE_PARAMS.from_address(msg.lParam).rgrc[0]
                r.left += thick
                r.top += thick
                r.right -= thick
                r.bottom -= thick
            return True, 0
        if msg.message == WM_NCHITTEST:
            code = self._hit_test()
            if code is not None:
                return True, code
        return None

    def _hit_test(self) -> int | None:
        win = self._win
        pos = win.mapFromGlobal(QCursor.pos())
        w, h, b = win.width(), win.height(), self._border
        if not win.isMaximized():
            left, right, top, bottom = pos.x() < b, pos.x() >= w - b, pos.y() < b, pos.y() >= h - b
            if top and left:
                return HTTOPLEFT
            if top and right:
                return HTTOPRIGHT
            if bottom and left:
                return HTBOTTOMLEFT
            if bottom and right:
                return HTBOTTOMRIGHT
            if left:
                return HTLEFT
            if right:
                return HTRIGHT
            if top:
                return HTTOP
            if bottom:
                return HTBOTTOM
        if self._is_caption(pos):
            return HTCAPTION
        return None

    # ---------------------------------------------------- alternativa (no Windows)
    def mouse_press(self, local: QPoint) -> bool:
        if self._native_ok or not self._is_caption(local):
            return False
        handle = self._win.windowHandle()
        return bool(handle and handle.startSystemMove())


class WindowOutline(QWidget):
    """Borde fino y redondeado de la ventana, dibujado por encima de todo (no recibe clics)."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.radius = CORNER_RADIUS
        parent.installEventFilter(self)
        self.setGeometry(parent.rect())

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt)
        if event.type() == QEvent.Resize:
            self.setGeometry(obj.rect())
            self.raise_()
        return False

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt)
        if self.radius <= 0:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QColor(t.BORDER_HI))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), self.radius, self.radius)


def corner_styles(radius: int) -> dict[str, str]:
    """Hojas de estilo de las piezas que tocan las esquinas (barra lateral, cabecera, barra inferior)."""
    r = f"{radius}px"
    return {
        "sidebar": f"QFrame#sidebar {{ background: {t.BG_950}; border-right: 1px solid {t.BORDER};"
                   f" border-top-left-radius: {r}; border-bottom-left-radius: {r}; }}",
        "header": f"QFrame#header {{ background: {t.BG_900}; border-bottom: 1px solid {t.BORDER};"
                  f" border-top-right-radius: {r}; }}",
        "nowBar": f"QFrame#nowBar {{ background: {t.BG_950}; border-top: 1px solid {t.BORDER};"
                  f" border-bottom-right-radius: {r}; }}",
    }


class WindowButtons(QWidget):
    """Minimizar / maximizar / cerrar con el estilo de la app."""

    def __init__(self, window: QWidget) -> None:
        super().__init__()
        self._win = window
        self._min = icon_button("minus", "Minimizar", window.showMinimized, t.TEXT_2, 16, "winBtn")
        self._max = icon_button("square", "Maximizar", self._toggle_max, t.TEXT_2, 13, "winBtn")
        self._close = icon_button("x", "Cerrar", window.close, t.TEXT_2, 16, "winClose")
        for b in (self._min, self._max, self._close):
            b.setFixedSize(QSize(40, 32))
        self.setLayout(hbox(self._min, self._max, self._close, spacing=2))

    def _toggle_max(self) -> None:
        if self._win.isMaximized():
            self._win.showNormal()
        else:
            self._win.showMaximized()

    def update_state(self) -> None:
        maximized = self._win.isMaximized()
        self._max.setIcon(t.icon("copy" if maximized else "square", t.TEXT_2, 13))
        self._max.setToolTip("Restaurar" if maximized else "Maximizar")
