"""Acceso a secretos mediante el Administrador de credenciales de Windows (keyring).

Cada secreto se guarda bajo el servicio ``AlpekaBOT/<clave>``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import keyring
from keyring.errors import PasswordDeleteError

from . import APP_NAME

log = logging.getLogger(__name__)


def _service(key: str) -> str:
    return f"{APP_NAME}/{key}"


def get_secret(key: str) -> str | None:
    return keyring.get_password(_service(key), APP_NAME)


def set_secret(key: str, value: str) -> None:
    keyring.set_password(_service(key), APP_NAME, value)


def delete_secret(key: str) -> None:
    try:
        keyring.delete_password(_service(key), APP_NAME)
    except PasswordDeleteError:
        pass  # no existía


def get_json(key: str) -> dict[str, Any] | None:
    raw = get_secret(key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.warning("El secreto %s está corrupto; se descarta", key)
        delete_secret(key)
        return None


def set_json(key: str, value: dict[str, Any]) -> None:
    set_secret(key, json.dumps(value, separators=(",", ":")))
