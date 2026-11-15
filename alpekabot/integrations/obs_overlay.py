"""Servidor del overlay de OBS: una página y un WebSocket en ``127.0.0.1:<puerto fijo>``.

En OBS: Fuente → Navegador → la URL de ``OverlayServer.url``. Solo escucha en este PC y
la URL lleva un token, para que ninguna web abierta en el navegador pueda usarlo.
El puerto es fijo (``config.overlay.port``) para que la URL puesta en OBS no cambie.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from pathlib import Path

from aiohttp import WSMsgType, web

from ..config import OverlayConfig
from ..features import overlay as ov
from ..features.songs import SongStatus

log = logging.getLogger(__name__)

ASSETS = Path(__file__).resolve().parent.parent / "assets"
PAGE_FILE = ASSETS / "overlay" / "now_playing.html"
FONTS_DIR = ASSETS / "fonts"
PAGE_PATH = "/overlay/now-playing"
WS_PATH = "/overlay/ws"


class OverlayServer:
    def __init__(self, config: OverlayConfig, token: str | None = None) -> None:
        self.config = config
        self.token = token or secrets.token_urlsafe(18)
        self._runner: web.AppRunner | None = None
        self._clients: dict[web.WebSocketResponse, bool] = {}  # ws → es la vista previa del editor
        self._status: SongStatus | None = None
        self._progress = (0.0, 0.0)
        self.port: int | None = None     # puerto en uso (None = apagado)
        self.last_error = ""

    # ------------------------------------------------------------- estado
    @property
    def running(self) -> bool:
        return self._runner is not None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.config.port}{PAGE_PATH}?t={self.token}"

    @property
    def preview_url(self) -> str:
        return self.url + "&preview=1"

    # ------------------------------------------------------- arranque/cierre
    async def start(self) -> bool:
        """Arranca si está activado. Devuelve si quedó escuchando (el motivo queda en ``last_error``)."""
        await self.stop()
        self.last_error = ""
        if not self.config.enabled:
            return False
        ov.sanitize(self.config)
        app = web.Application()
        app.add_routes([
            web.get(PAGE_PATH, self._page),
            web.get(WS_PATH, self._ws),
            web.get("/overlay/fonts/{name}", self._font),
        ])
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, "127.0.0.1", self.config.port).start()  # solo este PC
        except OSError:
            await runner.cleanup()
            self.last_error = f"El puerto {self.config.port} está ocupado por otro programa. Elige otro."
            log.error("Overlay de OBS: %s", self.last_error)
            return False
        self._runner = runner
        self.port = self.config.port
        log.info("Overlay de OBS listo en el puerto %s", self.port)
        return True

    async def stop(self) -> None:
        for ws in list(self._clients):
            try:
                await ws.close()
            except Exception:  # noqa: BLE001 (cierre: lo que falle da igual)
                pass
        self._clients.clear()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self.port = None

    # ------------------------------------------------------- datos de la app
    def set_status(self, status: SongStatus) -> None:
        song_changed = (self._status is None or self._status.current is None or status.current is None
                        or self._status.current.video_id != status.current.video_id)
        self._status = status
        if song_changed:
            self._progress = (0.0, 0.0)
        self._broadcast_state()

    def set_progress(self, pos: float, dur: float) -> None:
        self._progress = (pos, dur)
        msg = ov.progress_message(pos, dur)
        has_song = self._status is not None and self._status.current is not None
        for ws, preview in list(self._clients.items()):
            if has_song or not preview:  # la vista previa sin canción mantiene su ejemplo
                self._send(ws, msg)

    def config_changed(self) -> None:
        """Reenvía el estilo a todas las páginas abiertas (OBS y la vista previa)."""
        msg = ov.config_message(self.config)
        for ws in list(self._clients):
            self._send(ws, msg)
        self._broadcast_state()

    # ------------------------------------------------------------- envío
    def _messages_for(self, preview: bool) -> list[dict]:
        state = ov.state_message(self._status, self.config, preview)
        has_song = self._status is not None and self._status.current is not None
        progress = self._progress if has_song or not preview else ov.SAMPLE_PROGRESS
        return [ov.config_message(self.config), state, ov.progress_message(*progress)]

    def _broadcast_state(self) -> None:
        for ws, preview in list(self._clients.items()):
            self._send(ws, ov.state_message(self._status, self.config, preview))

    def _send(self, ws: web.WebSocketResponse, msg: dict) -> None:
        if ws.closed:
            self._clients.pop(ws, None)
            return
        asyncio.ensure_future(self._send_async(ws, json.dumps(msg, ensure_ascii=False)))

    async def _send_async(self, ws: web.WebSocketResponse, text: str) -> None:
        try:
            await ws.send_str(text)
        except Exception:  # noqa: BLE001 (OBS cerró la fuente)
            self._clients.pop(ws, None)

    # ------------------------------------------------------------- rutas
    def _authorized(self, request: web.Request) -> bool:
        return secrets.compare_digest(request.query.get("t", ""), self.token)

    async def _page(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            raise web.HTTPForbidden()
        return web.Response(text=PAGE_FILE.read_text(encoding="utf-8"), content_type="text/html",
                            headers={"Cache-Control": "no-store"})

    async def _font(self, request: web.Request) -> web.FileResponse:
        name = request.match_info["name"]
        available = {p.name: p for p in FONTS_DIR.glob("*.ttf")}  # solo estos archivos, nada más
        if name not in available:
            raise web.HTTPNotFound()
        return web.FileResponse(available[name])

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        if not self._authorized(request):
            raise web.HTTPForbidden()
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=4096)
        await ws.prepare(request)
        preview = "preview" in request.query
        self._clients[ws] = preview
        for msg in self._messages_for(preview):
            self._send(ws, msg)
        async for m in ws:  # la página no envía nada: solo se espera a que se cierre
            if m.type == WSMsgType.ERROR:
                break
        self._clients.pop(ws, None)
        return ws
