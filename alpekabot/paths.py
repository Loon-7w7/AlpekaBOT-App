"""Rutas de la app.

Desde el código fuente, los datos viven en ``data/`` (excluido de git). Instalada,
en ``%LOCALAPPDATA%\\AlpekaBOT``: la carpeta del programa se reemplaza al actualizar.
"""

import os
import sys
from pathlib import Path

IS_FROZEN = bool(getattr(sys, "frozen", False))  # True en la versión empaquetada


def _data_dir() -> Path:
    if not IS_FROZEN:
        return Path(__file__).resolve().parent.parent / "data"
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) if base else Path.home() / "AppData" / "Local") / "AlpekaBOT"


DATA_DIR = _data_dir()
LOG_DIR = DATA_DIR / "logs"
UPDATE_DIR = DATA_DIR / "updates"  # instaladores descargados
CONFIG_FILE = DATA_DIR / "config.json"
DB_FILE = DATA_DIR / "alpekabot.db"
NOW_PLAYING_FILE = DATA_DIR / "now_playing.txt"  # para una fuente de texto en OBS


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(exist_ok=True)
