"""Cliente de la API pública de VTube Studio (WebSocket local, por defecto ``ws://127.0.0.1:8001``).

- Al conectar se autentica con el token guardado en keyring. Si no hay token (o VTS lo
  revocó), el estado pasa a «necesita autorización» y NO se pide solo: el usuario pulsa
  «Autorizar» en la app y acepta la ventana que abre VTube Studio. Así la reconexión
  automática no le llena la pantalla de ventanas.
- Si VTube Studio se cierra, se reintenta cada ``RECONNECT_S`` segundos.
- Todo corre en el bucle de Qt (qasync); no hay hilos.

Documentación: https://github.com/DenchiSoft/VTubeStudio
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Callable

import aiohttp

from .. import credentials
from ..config import VTubeStudioConfig

log = logging.getLogger(__name__)

PLUGIN_NAME = "AlpekaBOT"
PLUGIN_DEVELOPER = "AlpekaBOT"
TOKEN_KEY = "vtube/token"
RECONNECT_S = 10
REQUEST_TIMEOUT_S = 10
AUTH_TIMEOUT_S = 120          # tiempo para pulsar «Permitir» en VTube Studio
ERROR_USER_DENIED = 50


class VTSError(Exception):
    """Error con un mensaje apto para el usuario."""

    def __init__(self, message: str, error_id: int = -1) -> None:
        super().__init__(message)
        self.error_id = error_id


class VTSState(str, Enum):
    OFF = "off"
    CONNECTING = "connecting"
    NEEDS_AUTH = "needs_auth"
    CONNECTED = "connected"

    @property
    def label(self) -> str:
        return {"off": "Desactivado", "connecting": "Buscando VTube Studio…",
                "needs_auth": "Falta autorizar", "connected": "Conectado"}[self.value]


@dataclass(frozen=True)
class VTSStatus:
    state: VTSState = VTSState.OFF
    detail: str = ""
    model: str = ""


@dataclass(frozen=True)
class Hotkey:
    id: str
    name: str
    type: str = ""


@dataclass(frozen=True)
class Expression:
    file: str
    name: str
    active: bool = False


class VTubeStudioClient:
    def __init__(
        self,
        config: VTubeStudioConfig,
        on_status: Callable[[VTSStatus], None] | None = None,
        get_token: Callable[[], str | None] = lambda: credentials.get_secret(TOKEN_KEY),
        set_token: Callable[[str | None], None] | None = None,
    ) -> None:
        self._config = config
        self._on_status = on_status
        self._get_token = get_token
        self._set_token = set_token or _store_token
        self._status = VTSStatus()
        self._task: asyncio.Task | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._pending: dict[str, asyncio.Future] = {}

    # ------------------------------------------------------------- estado
    @property
    def status(self) -> VTSStatus:
        return self._status

    @property
    def is_connected(self) -> bool:
        return self._status.state is VTSState.CONNECTED

    @property
    def url(self) -> str:
        return f"ws://{self._config.host or '127.0.0.1'}:{int(self._config.port)}"

    def _set(self, state: VTSState, detail: str = "", model: str | None = None) -> None:
        status = VTSStatus(state, detail, self._status.model if model is None else model)
        if status != self._status:
            if state is not self._status.state:
                log.info("VTube Studio: %s %s", state.label, detail)
            self._status = status
            if self._on_status:
                self._on_status(status)

    # ------------------------------------------------------------ ciclo
    def start(self) -> None:
        """Empieza a buscar VTube Studio si está activado en la configuración."""
        if self._config.enabled and self._task is None:
            self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._set(VTSState.OFF, model="")

    async def restart(self) -> None:
        await self.stop()
        self.start()

    async def _run(self) -> None:
        while True:
            try:
                await self._session()
                detail = "VTube Studio se cerró"
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
                detail = f"no responde en {self.url} (¿está abierto con la API activada?)"
            except VTSError as exc:
                detail = str(exc)
            except Exception:
                log.exception("VTube Studio: error inesperado")
                detail = "error inesperado (mira la consola)"
            self._set(VTSState.CONNECTING, f"{detail}; reintento en {RECONNECT_S} s", model="")
            await asyncio.sleep(RECONNECT_S)

    async def _session(self) -> None:
        self._set(VTSState.CONNECTING, model="")
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=5)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.ws_connect(self.url, heartbeat=30, max_msg_size=8 * 1024 * 1024) as ws:
                self._ws = ws
                reader = asyncio.ensure_future(self._read(ws))
                try:
                    await self._authenticate()
                    await reader
                finally:
                    reader.cancel()
                    self._ws = None
                    for fut in self._pending.values():
                        if not fut.done():
                            fut.set_exception(VTSError("Se perdió la conexión con VTube Studio"))
                    self._pending.clear()

    async def _read(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for msg in ws:
            if msg.type is not aiohttp.WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            fut = self._pending.pop(str(data.get("requestID", "")), None)
            if fut is None or fut.done():
                continue
            body = data.get("data") or {}
            if data.get("messageType") == "APIError":
                fut.set_exception(VTSError(_error_text(body), int(body.get("errorID", -1))))
            else:
                fut.set_result(body)

    # ------------------------------------------------------ autenticación
    async def _authenticate(self) -> None:
        token = self._get_token()
        if not token:
            self._set(VTSState.NEEDS_AUTH, "pulsa «Autorizar» y acepta el aviso en VTube Studio")
            return
        resp = await self.request("AuthenticationRequest", {
            "pluginName": PLUGIN_NAME, "pluginDeveloper": PLUGIN_DEVELOPER, "authenticationToken": token,
        })
        if not resp.get("authenticated"):
            self._set_token(None)  # revocado en VTube Studio
            self._set(VTSState.NEEDS_AUTH, "VTube Studio retiró el permiso: vuelve a autorizar")
            return
        await self._connected()

    async def _connected(self) -> None:
        model = ""
        try:
            model = str((await self.request("CurrentModelRequest")).get("modelName", ""))
        except VTSError:
            pass
        self._set(VTSState.CONNECTED, model=model)

    async def authorize(self) -> None:
        """Pide permiso a VTube Studio (sale una ventana allí). Lanza VTSError si no se concede."""
        if self._ws is None:
            raise VTSError(f"VTube Studio no está abierto o no tiene la API activada ({self.url}).")
        try:
            resp = await self.request("AuthenticationTokenRequest", {
                "pluginName": PLUGIN_NAME, "pluginDeveloper": PLUGIN_DEVELOPER,
            }, timeout=AUTH_TIMEOUT_S)
        except VTSError as exc:
            if exc.error_id == ERROR_USER_DENIED:
                raise VTSError("Rechazaste el permiso en VTube Studio.", exc.error_id) from None
            raise
        token = str(resp.get("authenticationToken", ""))
        if not token:
            raise VTSError("VTube Studio no devolvió un token.")
        self._set_token(token)
        auth = await self.request("AuthenticationRequest", {
            "pluginName": PLUGIN_NAME, "pluginDeveloper": PLUGIN_DEVELOPER, "authenticationToken": token,
        })
        if not auth.get("authenticated"):
            raise VTSError("VTube Studio no aceptó el token nuevo.")
        await self._connected()

    # ------------------------------------------------------------ peticiones
    async def request(self, message_type: str, data: dict | None = None, *,
                      timeout: float = REQUEST_TIMEOUT_S, need_auth: bool = False) -> dict:
        ws = self._ws
        if ws is None or ws.closed or (need_auth and not self.is_connected):
            raise VTSError("VTube Studio no está conectado.")
        rid = uuid.uuid4().hex[:16]
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        msg = {"apiName": "VTubeStudioPublicAPI", "apiVersion": "1.0", "requestID": rid,
               "messageType": message_type}
        if data is not None:
            msg["data"] = data
        try:
            await ws.send_str(json.dumps(msg))
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise VTSError("VTube Studio no respondió a tiempo.") from None
        except (aiohttp.ClientError, ConnectionError):
            raise VTSError("Se perdió la conexión con VTube Studio.") from None
        finally:
            self._pending.pop(rid, None)

    async def hotkeys(self) -> list[Hotkey]:
        data = await self.request("HotkeysInCurrentModelRequest", {}, need_auth=True)
        return [Hotkey(str(h.get("hotkeyID", "")), str(h.get("name", "")) or str(h.get("type", "")),
                       str(h.get("type", "")))
                for h in data.get("availableHotkeys") or [] if h.get("hotkeyID")]

    async def expressions(self) -> list[Expression]:
        data = await self.request("ExpressionStateRequest", {"details": False}, need_auth=True)
        return [Expression(str(e.get("file", "")), str(e.get("name", "")), bool(e.get("active")))
                for e in data.get("expressions") or [] if e.get("file")]

    async def trigger_hotkey(self, hotkey_id: str) -> None:
        await self.request("HotkeyTriggerRequest", {"hotkeyID": hotkey_id}, need_auth=True)

    async def set_expression(self, file: str, active: bool, fade_s: float = 0.25) -> None:
        data = {"expressionFile": file, "active": bool(active)}
        if active:
            data["fadeTime"] = max(0.0, min(2.0, fade_s))
        await self.request("ExpressionActivationRequest", data, need_auth=True)


def _store_token(token: str | None) -> None:
    if token:
        credentials.set_secret(TOKEN_KEY, token)
    else:
        credentials.delete_secret(TOKEN_KEY)


def _error_text(body: dict) -> str:
    text = str(body.get("message", "")).strip()[:200]
    return f"VTube Studio: {text}" if text else "VTube Studio devolvió un error."
