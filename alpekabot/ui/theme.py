"""Tema de AlpekaBOT: colores, fuentes (Inter + JetBrains Mono), iconos Lucide y hoja de estilos.

Basado en el diseño de ``diseño/stitch_minimalist_desktop_ui_design``. Los colores
se definen una sola vez aquí; los widgets usan estas constantes o nombres de
objeto (``setObjectName``) que la hoja de estilos reconoce.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

log = logging.getLogger(__name__)

ASSETS = Path(__file__).resolve().parent.parent / "assets"

# ------------------------------------------------------------------ colores
BG_950 = "#090a0d"
BG_900 = "#0d0e12"
BG_850 = "#111318"
BG_800 = "#161922"
BG_700 = "#1e222e"
BG_600 = "#272d3d"
BORDER = "#232735"
BORDER_HI = "#3b435b"

TEXT = "#e5e7eb"
TEXT_2 = "#9ca3af"
TEXT_3 = "#6b7280"
TEXT_4 = "#4b5563"

ACCENT = "#9146ff"        # morado (Twitch)
ACCENT_HOVER = "#a970ff"
ACCENT_SOFT = "#c4a5ff"
KICK = "#53fc18"
GREEN = "#22c55e"
AMBER = "#f59e0b"
GOLD = "#eab308"
RED = "#ef4444"
BLUE = "#60a5fa"
PINK = "#f472b6"
ORANGE = "#fb923c"

PLATFORM_COLORS = {"twitch": ACCENT, "kick": KICK}
STATE_COLORS = {
    "disconnected": TEXT_3,
    "connecting": AMBER,
    "connected": GREEN,
    "error": RED,
}

SANS = "Inter"
MONO = "JetBrains Mono"


def alpha(color: str, a: float) -> str:
    """'#9146ff', 0.15 -> 'rgba(145,70,255,0.15)' (para la hoja de estilos)."""
    c = QColor(color)
    return f"rgba({c.red()},{c.green()},{c.blue()},{a:g})"


def qcolor(color: str, a: float = 1.0) -> QColor:
    c = QColor(color)
    c.setAlphaF(a)
    return c


# ------------------------------------------------------------------ fuentes
def load_fonts() -> None:
    for path in sorted((ASSETS / "fonts").glob("*.ttf")):
        if QFontDatabase.addApplicationFont(str(path)) < 0:
            log.warning("No se pudo cargar la fuente %s", path.name)


def font(size: int = 13, weight: QFont.Weight = QFont.Normal, mono: bool = False) -> QFont:
    f = QFont(MONO if mono else SANS)
    f.setPixelSize(size)
    f.setWeight(weight)
    f.setStyleStrategy(QFont.PreferAntialias)
    return f


# ------------------------------------------------------------------- iconos
@lru_cache(maxsize=512)
def _svg(name: str) -> str:
    path = ASSETS / "icons" / f"{name}.svg"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        log.warning("Falta el icono %s", name)
        return ""


@lru_cache(maxsize=1024)
def pixmap(name: str, color: str = TEXT_2, size: int = 16, stroke: float = 2.0) -> QPixmap:
    """Icono Lucide coloreado, nítido en pantallas con escalado (devicePixelRatio)."""
    ratio = 2.0
    pm = QPixmap(int(size * ratio), int(size * ratio))
    pm.fill(Qt.transparent)
    svg = _svg(name)
    if svg:
        svg = svg.replace("currentColor", QColor(color).name()).replace(
            'stroke-width="2"', f'stroke-width="{stroke}"'
        )
        renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        renderer.render(p, QRectF(0, 0, size * ratio, size * ratio))
        p.end()
    pm.setDevicePixelRatio(ratio)
    return pm


def icon(name: str, color: str = TEXT_2, size: int = 16, disabled: str | None = None) -> QIcon:
    ic = QIcon(pixmap(name, color, size))
    ic.addPixmap(pixmap(name, disabled or TEXT_4, size), QIcon.Disabled)
    return ic


# ------------------------------------------------------------ hoja de estilos
_CHECK = (ASSETS / "icons" / "check-white.svg").as_posix()

_STYLESHEET = f"""
* {{ font-family: "{SANS}"; font-size: 13px; color: {TEXT}; }}
QMainWindow, QDialog {{ background: {BG_950}; }}
QWidget#appRoot {{ background: {BG_950}; }}
QWidget#content, QStackedWidget#pages, QWidget#page {{ background: {BG_900}; }}
QToolTip {{ background: {BG_700}; color: {TEXT}; border: 1px solid {BORDER_HI}; padding: 5px 8px; border-radius: 6px; }}
QLabel {{ background: transparent; }}
QLabel#muted {{ color: {TEXT_3}; }}
QLabel#mono {{ font-family: "{MONO}"; color: {TEXT_2}; font-size: 12px; }}
QLabel#sectionTitle {{ font-family: "{MONO}"; font-size: 12px; font-weight: 600; color: {TEXT}; letter-spacing: 1px; }}
QLabel#sideSection {{ font-family: "{MONO}"; font-size: 11px; color: {TEXT_3}; letter-spacing: 1.5px; padding: 14px 10px 4px 10px; }}
QLabel#crumbRoot {{ font-family: "{MONO}"; color: {TEXT_3}; }}
QLabel#crumbPage {{ font-family: "{MONO}"; font-weight: 600; }}

/* ---------- barra lateral y cabecera ---------- */
QFrame#sidebar {{ background: {BG_950}; border-right: 1px solid {BORDER}; }}
QFrame#header {{ background: {BG_900}; border-bottom: 1px solid {BORDER}; }}
QFrame#hline {{ background: {BORDER}; max-height: 1px; min-height: 1px; border: none; }}
QPushButton#nav {{
    text-align: left; padding: 9px 12px; border-radius: 10px; border: 1px solid transparent;
    background: transparent; color: {TEXT_2}; font-size: 14px;
}}
QPushButton#nav:hover {{ background: {BG_850}; color: {TEXT}; }}
QPushButton#nav:checked {{ background: {alpha(ACCENT, 0.14)}; border-color: {alpha(ACCENT, 0.55)}; color: white; }}
QPushButton#nav:disabled {{ color: {TEXT_4}; }}

/* ---------- tarjetas ---------- */
QFrame#card {{ background: {BG_850}; border: 1px solid {BORDER}; border-radius: 12px; }}
QFrame#cardHi {{ background: {BG_850}; border: 1px solid {alpha(ACCENT, 0.45)}; border-radius: 14px; }}
QFrame#cardInner {{ background: {BG_900}; border: 1px solid {BORDER}; border-radius: 10px; }}
QFrame#row {{ background: {BG_850}; border: 1px solid {BORDER}; border-radius: 12px; }}
QFrame#row:hover {{ border-color: {BORDER_HI}; }}
QFrame#modalCard {{ background: {BG_850}; border: 1px solid {alpha(ACCENT, 0.35)}; border-radius: 16px; }}
QFrame#modalFooter {{ border: none; border-top: 1px solid {BORDER}; border-radius: 0; background: transparent; }}
QFrame#modalHeader {{ border: none; border-bottom: 1px solid {BORDER}; border-radius: 0; background: transparent; }}

/* ---------- botones ---------- */
QPushButton {{
    background: {BG_800}; border: 1px solid {BORDER}; border-radius: 8px; padding: 7px 14px; color: {TEXT};
}}
QPushButton:hover {{ background: {BG_700}; border-color: {BORDER_HI}; }}
QPushButton:pressed {{ background: {BG_600}; }}
QPushButton:disabled {{ color: {TEXT_4}; background: {BG_850}; border-color: {BORDER}; }}
QPushButton#primary {{ background: {ACCENT}; border-color: {ACCENT}; color: white; font-weight: 600; }}
QPushButton#primary:hover {{ background: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}
QPushButton#primary:disabled {{ background: {alpha(ACCENT, 0.35)}; border-color: transparent; color: {TEXT_3}; }}
QPushButton#outlineAccent {{ background: {alpha(ACCENT, 0.08)}; border-color: {alpha(ACCENT, 0.45)}; color: {ACCENT_SOFT}; }}
QPushButton#outlineAccent:hover {{ background: {alpha(ACCENT, 0.18)}; }}
QPushButton#danger {{ background: {alpha(RED, 0.10)}; border-color: {alpha(RED, 0.45)}; color: #fca5a5; }}
QPushButton#danger:hover {{ background: {alpha(RED, 0.20)}; }}
QPushButton#success {{ background: {alpha(GREEN, 0.10)}; border-color: {alpha(GREEN, 0.45)}; color: #86efac;
    font-family: "{MONO}"; font-size: 12px; }}
QPushButton#success:hover {{ background: {alpha(GREEN, 0.18)}; }}
QPushButton#ghost {{ background: transparent; border-color: transparent; color: {TEXT_2}; }}
QPushButton#ghost:hover {{ background: {BG_800}; color: {TEXT}; }}
QPushButton#iconBtn {{ background: transparent; border: 1px solid transparent; border-radius: 8px; padding: 6px; }}
QPushButton#iconBtn:hover {{ background: {BG_700}; border-color: {BORDER}; }}
QPushButton#iconBtn:disabled {{ background: transparent; border-color: transparent; }}
QPushButton#iconBox {{ background: {BG_800}; border: 1px solid {BORDER}; border-radius: 8px; padding: 6px; }}
QPushButton#iconBox:hover {{ background: {BG_700}; border-color: {BORDER_HI}; }}
QPushButton#playBtn {{ background: {ACCENT}; border: none; border-radius: 18px; padding: 0; }}
QPushButton#playBtn:hover {{ background: {ACCENT_HOVER}; }}
QPushButton#playBtn:disabled {{ background: {alpha(ACCENT, 0.35)}; }}
QPushButton#chip {{
    background: {BG_850}; border: 1px solid {BORDER}; border-radius: 8px; padding: 6px 14px; color: {TEXT_2};
}}
QPushButton#chip:hover {{ color: {TEXT}; border-color: {BORDER_HI}; }}
QPushButton#chip:checked {{ background: {alpha(ACCENT, 0.14)}; border-color: {alpha(ACCENT, 0.6)}; color: white; font-weight: 600; }}
QPushButton#varChip {{
    background: {BG_900}; border: 1px solid {BORDER}; border-radius: 6px; padding: 3px 9px;
    font-family: "{MONO}"; font-size: 12px; color: {ACCENT_SOFT};
}}
QPushButton#varChip:hover {{ border-color: {alpha(ACCENT, 0.6)}; }}

/* botones de la ventana (sin marco) */
QPushButton#winBtn {{ background: transparent; border: none; border-radius: 6px; padding: 7px 10px; }}
QPushButton#winBtn:hover {{ background: {BG_700}; }}
QPushButton#winClose {{ background: transparent; border: none; border-radius: 6px; padding: 7px 10px; }}
QPushButton#winClose:hover {{ background: #c42b1c; }}

/* ---------- campos ---------- */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QComboBox {{
    background: {BG_900}; border: 1px solid {BORDER}; border-radius: 8px; padding: 7px 10px;
    selection-background-color: {alpha(ACCENT, 0.5)};
}}
QLineEdit:hover, QPlainTextEdit:hover, QSpinBox:hover, QComboBox:hover {{ border-color: {BORDER_HI}; }}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QComboBox:focus {{ border-color: {alpha(ACCENT, 0.8)}; }}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{ color: {TEXT_4}; }}
QLineEdit#mono, QPlainTextEdit#mono {{ font-family: "{MONO}"; font-size: 12px; }}
QLineEdit#search {{ background: {BG_850}; }}
QLineEdit#composer {{ background: {BG_850}; padding: 11px 14px; font-size: 14px; border-radius: 10px; }}
QSpinBox::up-button, QSpinBox::down-button {{ width: 0; border: none; }}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox::down-arrow {{ image: url({(ASSETS / "icons" / "chevron-down-gray.svg").as_posix()}); width: 12px; height: 12px; }}
QComboBox QAbstractItemView {{
    background: {BG_800}; border: 1px solid {BORDER_HI}; border-radius: 8px; padding: 4px; outline: none;
    selection-background-color: {alpha(ACCENT, 0.25)};
}}

QCheckBox {{ spacing: 8px; color: {TEXT}; background: transparent; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid {BORDER_HI}; background: {BG_900}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; image: url({_CHECK}); }}

QSlider {{ background: transparent; }}
QSlider::groove:horizontal {{ height: 4px; background: {BG_600}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{ width: 12px; height: 12px; margin: -4px 0; border-radius: 6px; background: white; }}
QSlider::handle:horizontal:hover {{ background: {ACCENT_SOFT}; }}

QProgressBar {{ background: {BG_600}; border: none; border-radius: 3px; max-height: 6px; min-height: 6px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}

/* ---------- listas, tablas, menús ---------- */
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QListView, QTableWidget, QTableView {{
    background: {BG_900}; border: none; outline: none; alternate-background-color: {BG_850};
    selection-background-color: {alpha(ACCENT, 0.18)}; selection-color: {TEXT};
}}
QTableWidget, QTableView {{ border: 1px solid {BORDER}; border-radius: 10px; gridline-color: transparent; }}
QTableWidget::item, QTableView::item {{ padding: 6px 8px; border-bottom: 1px solid {BORDER}; }}
QHeaderView::section {{
    background: {BG_850}; color: {TEXT_3}; border: none; border-bottom: 1px solid {BORDER};
    padding: 8px; font-family: "{MONO}"; font-size: 11px;
}}
QTableCornerButton::section {{ background: {BG_850}; border: none; }}
QMenu {{ background: {BG_800}; border: 1px solid {BORDER_HI}; border-radius: 10px; padding: 6px; }}
QMenu::item {{ padding: 7px 18px 7px 12px; border-radius: 6px; }}
QMenu::item:selected {{ background: {alpha(ACCENT, 0.22)}; }}
QMenu::item:disabled {{ color: {TEXT_3}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 5px 6px; }}

QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BG_600}; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {BORDER_HI}; }}
QScrollBar:horizontal {{ background: transparent; height: 8px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {BG_600}; border-radius: 3px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QTabWidget::pane {{ border: none; }}
QTabBar::tab {{
    background: transparent; color: {TEXT_2}; padding: 8px 16px; margin-right: 6px;
    border: 1px solid {BORDER}; border-radius: 8px;
}}
QTabBar::tab:hover {{ color: {TEXT}; }}
QTabBar::tab:selected {{ background: {alpha(ACCENT, 0.14)}; border-color: {alpha(ACCENT, 0.6)}; color: white; }}
QGroupBox {{ border: 1px solid {BORDER}; border-radius: 10px; margin-top: 14px; padding: 12px 10px 10px 10px; background: {BG_850}; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px; color: {TEXT_2};
    font-family: "{MONO}"; font-size: 12px; }}
"""


def apply_dark_theme(app: QApplication) -> None:
    load_fonts()
    app.setStyle("Fusion")
    app.setFont(font(13))
    p = QPalette()
    for role, color in (
        (QPalette.Window, BG_900), (QPalette.WindowText, TEXT), (QPalette.Base, BG_900),
        (QPalette.AlternateBase, BG_850), (QPalette.ToolTipBase, BG_700), (QPalette.ToolTipText, TEXT),
        (QPalette.Text, TEXT), (QPalette.Button, BG_800), (QPalette.ButtonText, TEXT),
        (QPalette.Highlight, ACCENT), (QPalette.HighlightedText, "#ffffff"), (QPalette.Link, BLUE),
        (QPalette.PlaceholderText, TEXT_3),
    ):
        p.setColor(role, QColor(color))
    for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
        p.setColor(QPalette.Disabled, role, QColor(TEXT_4))
    app.setPalette(p)
    app.setStyleSheet(_STYLESHEET)
