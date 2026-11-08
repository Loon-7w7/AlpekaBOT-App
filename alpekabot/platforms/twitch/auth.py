"""Autenticación OAuth de Twitch para las dos cuentas (bot y streamer).

Los tokens se guardan en el Administrador de credenciales de Windows junto con
los datos básicos de la cuenta, para poder mostrarlos sin conectarse.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

from twitchAPI.helper import first
from twitchAPI.oauth import UserAuthenticator
from twitchAPI.twitch import Twitch
from twitchAPI.type import (
    AuthScope,
    InvalidRefreshTokenException,
    InvalidTokenException,
    MissingScopeException,
    TwitchAPIException,
)

from ... import credentials
from ...core.models import AccountInfo, AccountRole, Platform
from ..base import PlatformError, open_in_browser

log = logging.getLogger(__name__)

AUTH_TIMEOUT_S = 300  # 5 minutos para completar el login en el navegador

# Permisos mínimos por cuenta. Al añadir funciones (moderación, eventos...)
# se amplían aquí y la app pedirá volver a conectar la cuenta.
SCOPES: dict[AccountRole, list[AuthScope]] = {
    AccountRole.BOT: [
        AuthScope.USER_READ_CHAT,
        AuthScope.USER_WRITE_CHAT,
        AuthScope.USER_BOT,
        # Fase 5: el bot modera (debe ser moderador del canal: /mod <bot>)
        AuthScope.MODERATOR_MANAGE_BANNED_USERS,
        AuthScope.MODERATOR_MANAGE_CHAT_MESSAGES,
    ],
    AccountRole.STREAMER: [
        AuthScope.CHANNEL_BOT,
        # Fase 7: canjes (leer, crear premios, completar/devolver) y eventos del canal
        AuthScope.CHANNEL_READ_REDEMPTIONS,
        AuthScope.CHANNEL_MANAGE_REDEMPTIONS,
        AuthScope.MODERATOR_READ_FOLLOWERS,
        AuthScope.CHANNEL_READ_SUBSCRIPTIONS,
        AuthScope.BITS_READ,
    ],
}

_CLIENT_SECRET_KEY = "twitch/client_secret"


def _token_key(role: AccountRole) -> str:
    return f"twitch/account/{role.value}"


class NotAuthorizedError(PlatformError):
    def __init__(self, role: AccountRole, reason: str = "") -> None:
        msg = f"La cuenta de {role.label.lower()} de Twitch no está conectada"
        if reason:
            msg += f" ({reason})"
        super().__init__(msg + ". Conéctala en Configuración.")
        self.role = role


# ---------------------------------------------------------------- secretos
def get_client_secret() -> str:
    return credentials.get_secret(_CLIENT_SECRET_KEY) or ""


def set_client_secret(secret: str) -> None:
    if secret:
        credentials.set_secret(_CLIENT_SECRET_KEY, secret)
    else:
        credentials.delete_secret(_CLIENT_SECRET_KEY)


def load_account(role: AccountRole) -> AccountInfo | None:
    data = credentials.get_json(_token_key(role))
    if not data:
        return None
    return AccountInfo(Platform.TWITCH, role, data["user_id"], data["login"], data["display_name"])


def forget_account(role: AccountRole) -> None:
    credentials.delete_secret(_token_key(role))


def _save(role: AccountRole, access: str, refresh: str, info: AccountInfo) -> None:
    credentials.set_json(
        _token_key(role),
        {
            "access_token": access,
            "refresh_token": refresh,
            "user_id": info.user_id,
            "login": info.login,
            "display_name": info.display_name,
        },
    )


def _update_tokens(role: AccountRole, access: str, refresh: str) -> None:
    data = credentials.get_json(_token_key(role)) or {}
    data.update(access_token=access, refresh_token=refresh)
    credentials.set_json(_token_key(role), data)


def _require_app_credentials(client_id: str, secret: str) -> None:
    if not client_id or not secret:
        raise PlatformError(
            "Falta el Client ID o el Client Secret de Twitch. Configúralos en Configuración → Twitch."
        )


def _safe_error(exc: BaseException) -> str:
    """Descripción del error SIN su texto: twitchAPI incluye en él URLs con el client_secret."""
    return type(exc).__name__


async def _app_client(client_id: str, secret: str) -> Twitch:
    try:
        return await Twitch(client_id, secret)
    except TwitchAPIException as exc:
        log.warning("Twitch rechazó las credenciales de la app (%s)", _safe_error(exc))
        raise PlatformError(
            "Twitch rechazó el Client ID / Client Secret. Revisa que estén bien copiados."
        ) from None  # 'from None' evita arrastrar el mensaje original (contiene el secreto)


async def _current_user(twitch: Twitch, role: AccountRole) -> AccountInfo:
    user = await first(twitch.get_users())
    if user is None:
        raise PlatformError("Twitch no devolvió los datos de la cuenta")
    return AccountInfo(Platform.TWITCH, role, user.id, user.login, user.display_name)


# ------------------------------------------------------------------- login
async def authorize_account(client_id: str, secret: str, redirect_url: str, role: AccountRole) -> AccountInfo:
    """Abre el navegador para que el usuario inicie sesión con la cuenta indicada."""
    _require_app_credentials(client_id, secret)
    twitch = await _app_client(client_id, secret)
    port = urlparse(redirect_url).port or 80
    auth = UserAuthenticator(
        twitch,
        SCOPES[role],
        force_verify=True,  # permite elegir otra cuenta aunque ya haya sesión iniciada
        url=redirect_url,
        host="127.0.0.1",  # solo accesible desde este PC
        port=port,
    )
    try:
        # use_browser=False: abrimos nosotros el navegador en otro hilo (ver open_in_browser)
        token, refresh = await asyncio.wait_for(
            auth.authenticate(use_browser=False, auth_url_callback=open_in_browser), AUTH_TIMEOUT_S
        )
        await twitch.set_user_authentication(token, SCOPES[role], refresh)
        info = await _current_user(twitch, role)
        _save(role, token, refresh, info)
        log.info("Cuenta %s de Twitch conectada: %s", role.value, info.login)
        return info
    except (asyncio.TimeoutError, asyncio.CancelledError):
        _stop_quietly(auth)
        raise
    except TwitchAPIException as exc:
        _stop_quietly(auth)
        log.warning("Login de Twitch fallido (%s)", _safe_error(exc))
        raise PlatformError(
            "No se pudo completar el login de Twitch. Comprueba que la Redirect URL de tu app "
            "en dev.twitch.tv coincide exactamente con la de AlpekaBOT."
        ) from None
    finally:
        await twitch.close()


def _stop_quietly(auth: UserAuthenticator) -> None:
    try:
        auth.stop()
    except Exception:  # el servidor local puede no haber llegado a arrancar
        log.debug("UserAuthenticator ya estaba detenido", exc_info=True)


async def open_session(client_id: str, secret: str, role: AccountRole) -> tuple[Twitch, AccountInfo]:
    """Crea un cliente de Twitch autenticado con la cuenta guardada (renueva el token si caducó)."""
    _require_app_credentials(client_id, secret)
    data = credentials.get_json(_token_key(role))
    if not data:
        raise NotAuthorizedError(role)

    twitch = await _app_client(client_id, secret)

    async def on_refresh(access: str, refresh: str) -> None:
        log.debug("Token de %s renovado", role.value)
        _update_tokens(role, access, refresh)

    twitch.user_auth_refresh_callback = on_refresh
    try:
        await twitch.set_user_authentication(data["access_token"], SCOPES[role], data["refresh_token"])
        info = await _current_user(twitch, role)
    except MissingScopeException as exc:
        await twitch.close()
        raise NotAuthorizedError(role, "faltan permisos nuevos") from exc
    except (InvalidTokenException, InvalidRefreshTokenException) as exc:
        await twitch.close()
        raise NotAuthorizedError(role, "la sesión caducó") from exc
    except TwitchAPIException as exc:
        await twitch.close()
        log.warning("Error de Twitch abriendo la sesión de %s (%s)", role.value, _safe_error(exc))
        raise PlatformError(f"Twitch devolvió un error al abrir la sesión ({_safe_error(exc)})") from None
    except BaseException:
        await twitch.close()
        raise
    return twitch, info
