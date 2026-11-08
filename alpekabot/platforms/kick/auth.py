"""OAuth 2.1 + PKCE de Kick para las dos cuentas (bot y streamer).

Flujo: se abre el navegador en id.kick.com, Kick redirige a
http://localhost:<puerto>/callback con un ``code``, un mini servidor local lo
recoge (validando ``state``) y se canjea por tokens. Los tokens caducan y se
renuevan con el refresh token (TokenStore).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import secrets
import time
from urllib.parse import urlencode, urlparse

import aiohttp
from aiohttp import web

from ... import credentials
from ...core.models import AccountInfo, AccountRole, Platform
from ..base import PlatformError, open_in_browser
from .api import ID_BASE, KickAPI, KickAPIError

log = logging.getLogger(__name__)

AUTH_TIMEOUT_S = 300
REFRESH_MARGIN_S = 120  # renovar si caduca en menos de 2 minutos

SCOPES: dict[AccountRole, list[str]] = {
    AccountRole.BOT: ["user:read", "chat:write"],
    # El streamer se suscribe a los eventos de su canal y (fase 5) modera.
    AccountRole.STREAMER: [
        "user:read", "channel:read", "events:subscribe",
        "moderation:ban", "moderation:chat_message:manage",
        # Fase 7: premios (leer, crear, aceptar/rechazar canjes) y kicks regalados
        "channel:rewards:read", "channel:rewards:write", "kicks:read",
    ],
}
# Permisos que tenían los tokens guardados antes de que se registraran los scopes (fase 7)
_LEGACY_SCOPES: dict[AccountRole, list[str]] = {
    AccountRole.BOT: ["user:read", "chat:write"],
    AccountRole.STREAMER: ["user:read", "channel:read", "events:subscribe",
                           "moderation:ban", "moderation:chat_message:manage"],
}

_CLIENT_SECRET_KEY = "kick/client_secret"
NGROK_TOKEN_KEY = "ngrok/authtoken"

_DONE_HTML = """<!doctype html><meta charset="utf-8"><title>AlpekaBOT</title>
<body style="font-family:sans-serif;background:#18181d;color:#e6e6ea;text-align:center;padding-top:15vh">
<h2>{title}</h2><p>{text}</p></body>"""


def _token_key(role: AccountRole) -> str:
    return f"kick/account/{role.value}"


class NotAuthorizedError(PlatformError):
    def __init__(self, role: AccountRole, reason: str = "") -> None:
        msg = f"La cuenta de {role.label.lower()} de Kick no está conectada"
        if reason:
            msg += f" ({reason})"
        super().__init__(msg + ". Conéctala en Configuración → Kick.")
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
    return AccountInfo(Platform.KICK, role, data["user_id"], data["login"], data["display_name"])


def forget_account(role: AccountRole) -> None:
    credentials.delete_secret(_token_key(role))


# -------------------------------------------------------------------- PKCE
def make_pkce() -> tuple[str, str]:
    """Devuelve (code_verifier, code_challenge S256)."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorize_url(client_id: str, redirect_url: str, scopes: list[str], state: str, challenge: str) -> str:
    return ID_BASE + "oauth/authorize?" + urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_url,
        "scope": " ".join(scopes),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    })


async def wait_for_code(redirect_url: str, state: str, timeout: float = AUTH_TIMEOUT_S,
                        open_url: str | None = None) -> str:
    """Levanta un servidor en 127.0.0.1 y espera la redirección de Kick con el código.

    Si se indica ``open_url``, se abre en el navegador cuando el servidor ya escucha.
    """
    parsed = urlparse(redirect_url)
    port, path = parsed.port or 80, parsed.path or "/"
    loop = asyncio.get_running_loop()
    result: asyncio.Future[str] = loop.create_future()

    async def callback(request: web.Request) -> web.Response:
        q = request.query
        if not secrets.compare_digest(q.get("state", ""), state):
            return web.Response(text="state no válido", status=400)
        if "error" in q and not result.done():
            result.set_exception(PlatformError(f"Kick canceló el inicio de sesión ({q.get('error')})."))
            html = _DONE_HTML.format(title="Inicio de sesión cancelado", text="Puedes cerrar esta pestaña.")
            return web.Response(text=html, content_type="text/html")
        code = q.get("code", "")
        if code and not result.done():
            result.set_result(code)
        html = _DONE_HTML.format(title="✅ Cuenta conectada", text="Ya puedes cerrar esta pestaña y volver a AlpekaBOT.")
        return web.Response(text=html, content_type="text/html")

    app = web.Application()
    app.router.add_get(path, callback)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        try:
            await web.TCPSite(runner, "127.0.0.1", port).start()
        except OSError:
            raise PlatformError(f"El puerto {port} está ocupado. Cambia la Redirect URL de Kick.") from None
        if open_url:
            await open_in_browser(open_url)
        return await asyncio.wait_for(result, timeout)
    finally:
        await runner.cleanup()


async def _token_request(form: dict[str, str]) -> dict:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
            async with http.post(ID_BASE + "oauth/token", data=form) as r:
                status = r.status
                data = await r.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        log.warning("Kick OAuth: fallo de red (%s)", type(exc).__name__)
        raise PlatformError("No se pudo contactar con Kick.") from None
    if status != 200 or not isinstance(data, dict) or "access_token" not in data:
        err = data.get("error", "") if isinstance(data, dict) else ""
        log.warning("Kick OAuth: HTTP %s %s", status, err)
        raise KickAPIError(status, f"Kick rechazó la solicitud de token ({status} {err}).".replace(" ).", ")."))
    return data


def _check_credentials(client_id: str, secret: str) -> None:
    if not client_id or not secret:
        raise PlatformError("Falta el Client ID o el Client Secret de Kick (Configuración → Kick).")


def _save(role: AccountRole, tokens: dict, user: dict) -> AccountInfo:
    name = str(user.get("name", ""))
    info = AccountInfo(Platform.KICK, role, str(user["user_id"]), name.lower(), name)
    granted = str(tokens.get("scope", "")).split() or list(SCOPES[role])
    credentials.set_json(_token_key(role), {
        "scopes": granted,
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token", ""),
        "expires_at": time.time() + float(tokens.get("expires_in", 3600)),
        "user_id": info.user_id,
        "login": info.login,
        "display_name": info.display_name,
    })
    return info


async def authorize_account(client_id: str, secret: str, redirect_url: str, role: AccountRole) -> AccountInfo:
    _check_credentials(client_id, secret)
    verifier, challenge = make_pkce()
    state = secrets.token_urlsafe(24)
    url = build_authorize_url(client_id, redirect_url, SCOPES[role], state, challenge)
    code = await wait_for_code(redirect_url, state, open_url=url)
    tokens = await _token_request({
        "grant_type": "authorization_code", "client_id": client_id, "client_secret": secret,
        "redirect_uri": redirect_url, "code_verifier": verifier, "code": code,
    })
    granted = set(str(tokens.get("scope", "")).split())
    missing = [s for s in SCOPES[role] if granted and s not in granted]
    if missing:
        raise PlatformError(f"Kick no concedió todos los permisos ({', '.join(missing)}). Revisa los scopes de tu app.")
    api = KickAPI()
    try:
        user = await api.current_user(tokens["access_token"])
    finally:
        await api.close()
    info = _save(role, tokens, user)
    log.info("Cuenta %s de Kick conectada: %s", role.value, info.login)
    return info


class TokenStore:
    """Token de una cuenta con renovación automática (segura ante llamadas simultáneas)."""

    def __init__(self, role: AccountRole, client_id: str, secret: str) -> None:
        _check_credentials(client_id, secret)
        data = credentials.get_json(_token_key(role))
        if not data:
            raise NotAuthorizedError(role)
        granted = set(data.get("scopes") or _LEGACY_SCOPES[role])
        if any(s not in granted for s in SCOPES[role]):
            raise NotAuthorizedError(role, "faltan permisos nuevos")
        self.role = role
        self._client_id, self._secret = client_id, secret
        self._data = data
        self._lock = asyncio.Lock()

    @property
    def info(self) -> AccountInfo:
        d = self._data
        return AccountInfo(Platform.KICK, self.role, d["user_id"], d["login"], d["display_name"])

    async def access_token(self) -> str:
        if self._data.get("expires_at", 0) - time.time() < REFRESH_MARGIN_S:
            await self.refresh()
        return self._data["access_token"]

    async def refresh(self) -> None:
        old = self._data["access_token"]
        async with self._lock:
            if self._data["access_token"] != old:
                return  # otra tarea ya lo renovó
            if not self._data.get("refresh_token"):
                raise NotAuthorizedError(self.role, "la sesión caducó")
            try:
                tokens = await _token_request({
                    "grant_type": "refresh_token", "client_id": self._client_id,
                    "client_secret": self._secret, "refresh_token": self._data["refresh_token"],
                })
            except KickAPIError as exc:
                if exc.status in (400, 401):
                    raise NotAuthorizedError(self.role, "la sesión caducó") from None
                raise
            self._data.update(
                access_token=tokens["access_token"],
                refresh_token=tokens.get("refresh_token") or self._data["refresh_token"],
                expires_at=time.time() + float(tokens.get("expires_in", 3600)),
            )
            credentials.set_json(_token_key(self.role), self._data)
            log.debug("Token de Kick (%s) renovado", self.role.value)
