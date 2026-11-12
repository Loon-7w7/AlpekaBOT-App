"""Autenticación OAuth de Twitch para las dos cuentas (bot y streamer).

AlpekaBOT usa UNA app de Twitch compartida de tipo **Público** (sin client secret):
los usuarios solo inician sesión, con el flujo *Device Code* (la app muestra un
código y el usuario lo confirma en twitch.tv/activate).

Los tokens se guardan en el Administrador de credenciales de Windows junto con
los datos básicos de la cuenta, para poder mostrarlos sin conectarse.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import aiohttp
import twitchAPI.oauth
from twitchAPI.helper import TWITCH_AUTH_BASE_URL, build_scope, first
from twitchAPI.twitch import Twitch
from twitchAPI.type import (
    AuthScope,
    InvalidRefreshTokenException,
    InvalidTokenException,
    MissingScopeException,
    TwitchAPIException,
    UnauthorizedException,
)

from ... import credentials
from ...core.models import AccountInfo, AccountRole, Platform
from ..base import PlatformError, open_in_browser

log = logging.getLogger(__name__)

# App pública compartida de AlpekaBOT (dev.twitch.tv). El Client ID no es secreto.
CLIENT_ID = "ch3io8lbmfzl7payrhes0ugfk4dqmp"

AUTH_TIMEOUT_S = 600  # 10 minutos para escribir el código en twitch.tv/activate
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)

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

# Antes cada usuario ponía su propia app; su secreto ya no se usa y se borra.
_LEGACY_SECRET_KEY = "twitch/client_secret"


def _token_key(role: AccountRole) -> str:
    return f"twitch/account/{role.value}"


class NotAuthorizedError(PlatformError):
    def __init__(self, role: AccountRole, reason: str = "") -> None:
        msg = f"La cuenta de {role.label.lower()} de Twitch no está conectada"
        if reason:
            msg += f" ({reason})"
        super().__init__(msg + ". Conéctala en Configuración.")
        self.role = role


# ------------------------------------------------- renovación sin secreto
async def _refresh_public(refresh_token: str, app_id: str, app_secret: str | None = None,
                          session: aiohttp.ClientSession | None = None,
                          auth_base_url: str = TWITCH_AUTH_BASE_URL) -> tuple[str, str]:
    """Igual que ``twitchAPI.oauth.refresh_access_token`` pero sin mandar ``client_secret``
    cuando no hay (una app pública no tiene; twitchAPI enviaría el texto "None").

    Twitch rota el refresh token en cada uso: el nuevo lo guarda ``on_refresh``.
    """
    param = {"refresh_token": refresh_token, "client_id": app_id, "grant_type": "refresh_token"}
    if app_secret:
        param["client_secret"] = app_secret
    ses = session if session is not None else aiohttp.ClientSession(timeout=HTTP_TIMEOUT)
    try:
        async with ses.post(auth_base_url + "token", data=param) as result:
            data = await result.json(content_type=None)
    finally:
        if session is None:
            await ses.close()
    status = data.get("status", 200)
    if status == 400:
        raise InvalidRefreshTokenException(data.get("message", ""))
    if status == 401:
        raise UnauthorizedException(data.get("message", ""))
    return data["access_token"], data["refresh_token"]


# twitch.py importa la función desde el módulo en cada renovación
# (``from .oauth import refresh_access_token``), así que basta con sustituirla aquí.
twitchAPI.oauth.refresh_access_token = _refresh_public


# ---------------------------------------------------------------- cuentas
def cleanup_legacy_secret() -> None:
    if credentials.get_secret(_LEGACY_SECRET_KEY):
        credentials.delete_secret(_LEGACY_SECRET_KEY)
        log.info("Twitch: borrado el client secret antiguo (ya no hace falta)")


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


def _safe_error(exc: BaseException) -> str:
    """Descripción del error SIN su texto (twitchAPI puede incluir URLs con credenciales)."""
    return type(exc).__name__


async def _client() -> Twitch:
    # Sin secreto no hay token de app: todo va con el token del usuario
    return await Twitch(CLIENT_ID, authenticate_app=False)


async def _current_user(twitch: Twitch, role: AccountRole) -> AccountInfo:
    user = await first(twitch.get_users())
    if user is None:
        raise PlatformError("Twitch no devolvió los datos de la cuenta")
    return AccountInfo(Platform.TWITCH, role, user.id, user.login, user.display_name)


# ------------------------------------------------------------ Device Code
@dataclass(frozen=True)
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: float
    interval: float


async def request_device_code(session: aiohttp.ClientSession, scopes: list[AuthScope],
                              base_url: str = TWITCH_AUTH_BASE_URL) -> DeviceCode:
    data = {"client_id": CLIENT_ID, "scopes": build_scope(scopes)}
    async with session.post(base_url + "device", data=data) as resp:
        body = await resp.json(content_type=None)
    if resp.status != 200 or "device_code" not in body:
        log.warning("Twitch rechazó la petición de código (HTTP %s: %s)", resp.status, body.get("message"))
        raise PlatformError("Twitch no dio un código para iniciar sesión. Inténtalo de nuevo en un rato.")
    return DeviceCode(
        body["device_code"], body["user_code"], body["verification_uri"],
        float(body.get("expires_in", 1800)), float(body.get("interval", 5)),
    )


async def poll_device_token(session: aiohttp.ClientSession, code: DeviceCode, scopes: list[AuthScope],
                            base_url: str = TWITCH_AUTH_BASE_URL,
                            sleep: Callable[[float], object] = asyncio.sleep) -> tuple[str, str]:
    """Espera a que el usuario confirme el código. Lanza TimeoutError si caduca."""
    data = {
        "client_id": CLIENT_ID,
        "scopes": build_scope(scopes),
        "device_code": code.device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    }
    interval = max(code.interval, 1.0)
    deadline = time.monotonic() + min(code.expires_in, AUTH_TIMEOUT_S)
    while time.monotonic() < deadline:
        await sleep(interval)
        async with session.post(base_url + "token", data=data) as resp:
            body = await resp.json(content_type=None)
        if body.get("access_token"):
            return body["access_token"], body["refresh_token"]
        message = str(body.get("message", "")).lower()
        if "pending" in message:
            continue
        if "slow_down" in message:
            interval += 5
            continue
        if "denied" in message:
            raise PlatformError("Se canceló la autorización en Twitch.")
        if "invalid device code" in message or "expired" in message:
            break
        log.warning("Respuesta inesperada de Twitch al esperar el código (HTTP %s: %s)", resp.status, message)
        raise PlatformError("Twitch devolvió un error durante el inicio de sesión. Inténtalo de nuevo.")
    raise asyncio.TimeoutError


# ------------------------------------------------------------------- login
async def authorize_account(role: AccountRole,
                            on_code: Callable[[str, str], None] | None = None) -> AccountInfo:
    """Pide un código, abre twitch.tv/activate y espera a que el usuario lo confirme.

    ``on_code(código, url)`` permite a la interfaz mostrar el código mientras tanto.
    """
    scopes = SCOPES[role]
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
        try:
            code = await request_device_code(session, scopes)
            if on_code is not None:
                on_code(code.user_code, code.verification_uri)
            await open_in_browser(code.verification_uri)
            token, refresh = await poll_device_token(session, code, scopes)
        except aiohttp.ClientError as exc:
            log.warning("Sin conexión con Twitch al iniciar sesión (%s)", _safe_error(exc))
            raise PlatformError("No se pudo contactar con Twitch. Revisa tu conexión a internet.") from None

    twitch = await _client()
    try:
        await twitch.set_user_authentication(token, scopes, refresh)
        info = await _current_user(twitch, role)
        _save(role, token, refresh, info)
        log.info("Cuenta %s de Twitch conectada: %s", role.value, info.login)
        return info
    except TwitchAPIException as exc:
        log.warning("Login de Twitch fallido (%s)", _safe_error(exc))
        raise PlatformError("No se pudo completar el inicio de sesión de Twitch.") from None
    finally:
        await twitch.close()


async def open_session(role: AccountRole) -> tuple[Twitch, AccountInfo]:
    """Crea un cliente de Twitch autenticado con la cuenta guardada (renueva el token si caducó)."""
    data = credentials.get_json(_token_key(role))
    if not data:
        raise NotAuthorizedError(role)

    twitch = await _client()

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
    except InvalidTokenException as exc:
        await twitch.close()
        # Tokens de la app antigua de cada usuario: no valen con la app compartida
        reason = ("AlpekaBOT ahora usa su propia app de Twitch" if "client id" in str(exc).lower()
                  else "la sesión caducó")
        raise NotAuthorizedError(role, reason) from None
    except (InvalidRefreshTokenException, UnauthorizedException):
        await twitch.close()
        raise NotAuthorizedError(role, "la sesión caducó") from None
    except TwitchAPIException as exc:
        await twitch.close()
        log.warning("Error de Twitch abriendo la sesión de %s (%s)", role.value, _safe_error(exc))
        raise PlatformError(f"Twitch devolvió un error al abrir la sesión ({_safe_error(exc)})") from None
    except BaseException:
        await twitch.close()
        raise
    return twitch, info
