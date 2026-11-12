"""Conexión con el intermediario de AlpekaBOT para Kick (Cloudflare Worker, carpeta ``relay/``).

Con la app de Kick compartida:

* los tokens se piden y renuevan en ``<RELAY_BASE>oauth/token`` (el Worker añade el
  client_secret, que nunca llega a esta app);
* los webhooks de Kick llegan al Worker y este los reenvía por WebSocket a cada
  AlpekaBOT conectado al canal. Las cabeceras y el cuerpo llegan intactos, así que
  aquí se sigue verificando la firma RSA de Kick (``WebhookServer.process``).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol

import aiohttp

from ..base import PlatformError
from .api import KickUnauthorized

log = logging.getLogger(__name__)

# Worker desplegado con relay/wrangler.jsonc
RELAY_BASE = "https://alpekabot-kick.alpekabot-kick.workers.dev/"
# App de Kick compartida (el Client ID no es secreto) y su Redirect URL registrada en Kick
SHARED_CLIENT_ID = "01M6W2VRTN8KR46WBMCBX75MS6"
SHARED_REDIRECT_URL = "http://localhost:17564/callback"

HEARTBEAT_S = 30          # ping de protocolo (no gasta cuota en Cloudflare)
MAX_MESSAGE = 1024 * 1024
RETRY_MAX_S = 60

EventSink = Callable[[Mapping[str, str], bytes], Awaitable[object]]


class RelayNotAllowed(PlatformError):
    """El canal no está en la lista de canales permitidos del intermediario."""


class TokenSource(Protocol):
    """Lo mínimo que necesita la conexión: un token válido del streamer y poder renovarlo."""

    async def access_token(self) -> str: ...
    async def refresh(self) -> None: ...


class RelayConnection:
    """WebSocket con el Worker que reconecta solo si se cae."""

    def __init__(self, base_url: str, tokens: TokenSource, on_event: EventSink, channel: str = "",
                 sleep: Callable[[float], Awaitable[object]] = asyncio.sleep) -> None:
        self._url = base_url.rstrip("/") + "/ws"
        self._tokens = tokens
        self._on_event = on_event
        self._channel = channel
        self._sleep = sleep
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Primera conexión: los errores llegan a quien conecta (canal no autorizado, etc.)."""
        ws = await self._open()
        self._task = asyncio.ensure_future(self._run(ws))

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._session is not None:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------ interno
    async def _open(self) -> aiohttp.ClientWebSocketResponse:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, connect=15))
        try:
            return await self._connect()
        except KickUnauthorized:
            await self._tokens.refresh()  # token caducado: se renueva UNA vez
            return await self._connect()

    async def _connect(self) -> aiohttp.ClientWebSocketResponse:
        token = await self._tokens.access_token()
        try:
            return await self._session.ws_connect(
                self._url, headers={"Authorization": f"Bearer {token}"},
                heartbeat=HEARTBEAT_S, max_msg_size=MAX_MESSAGE,
            )
        except aiohttp.WSServerHandshakeError as exc:
            if exc.status == 401:
                raise KickUnauthorized(401, "Kick rechazó el token (sesión caducada).") from None
            if exc.status == 403:
                who = f"«{self._channel}»" if self._channel else "tu canal"
                raise RelayNotAllowed(
                    f"El servidor de AlpekaBOT para Kick no tiene autorizado {who}. "
                    "Pídele al administrador de AlpekaBOT que lo añada."
                ) from None
            log.warning("Intermediario de Kick: HTTP %s al conectar", exc.status)
            raise PlatformError(f"El servidor de AlpekaBOT para Kick respondió con un error ({exc.status}).") from None
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            log.warning("Intermediario de Kick: sin conexión (%s)", type(exc).__name__)
            raise PlatformError("No se pudo contactar con el servidor de AlpekaBOT para Kick.") from None

    async def _run(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        delay = 1.0
        while True:
            try:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._handle(msg.data)
                        delay = 1.0
                    elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                        break
            finally:
                await ws.close()
            log.warning("Kick: se perdió la conexión con el servidor de AlpekaBOT; reconectando…")
            while True:
                await self._sleep(delay)
                delay = min(delay * 2, RETRY_MAX_S)
                try:
                    ws = await self._open()
                except RelayNotAllowed as exc:
                    log.error("Kick: %s", exc)
                except PlatformError as exc:
                    log.warning("Kick: reconexión fallida (%s)", exc)
                else:
                    log.info("Kick: reconectado al servidor de AlpekaBOT")
                    break

    async def _handle(self, data: str) -> None:
        try:
            msg = json.loads(data)
        except ValueError:
            log.warning("Intermediario de Kick: mensaje no válido")
            return
        if not isinstance(msg, dict) or msg.get("type") != "event":
            return
        headers, body = msg.get("headers"), msg.get("body")
        if not isinstance(headers, dict) or not isinstance(body, str):
            return
        try:
            await self._on_event(headers, body.encode("utf-8"))
        except Exception:
            log.exception("Kick: error procesando un evento del intermediario")
