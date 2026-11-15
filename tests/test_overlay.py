import asyncio
import json
import socket

import aiohttp
import pytest

from alpekabot.config import OverlayConfig
from alpekabot.core.models import Platform
from alpekabot.features import overlay as ov
from alpekabot.features.songs import Song, SongStatus
from alpekabot.integrations.obs_overlay import OverlayServer


def song(**kw) -> Song:
    base = dict(video_id="dQw4w9WgXcQ", title="Canción <b>uno</b>", channel="Canal", duration_s=200, id=1,
                requested_by="ana", requester_id="1", platform=Platform.KICK)
    base.update(kw)
    return Song(**base)


def status(s: Song | None = None, *, backup=False, paused=False) -> SongStatus:
    return SongStatus(s, backup, paused, 50, True)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ------------------------------------------------------------------ lógica
def test_datos_de_la_cancion_pedida():
    p = ov.song_payload(status(song()))
    assert p == {"id": "dQw4w9WgXcQ", "title": "Canción <b>uno</b>", "channel": "Canal", "duration": 200,
                 "requester": "ana", "platform": "kick", "backup": False,
                 "cover": "https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg"}


def test_respaldo_sin_quien_la_pidio_ni_plataforma():
    p = ov.song_payload(status(song(requested_by="", platform=None), backup=True))
    assert (p["requester"], p["platform"], p["backup"]) == ("", None, True)


def test_id_raro_no_genera_url_de_portada():
    p = ov.song_payload(status(song(video_id='x"/><script>')))
    assert p["id"] == "" and p["cover"] == ""


@pytest.mark.parametrize("st, cfg, visible", [
    (None, {}, False),                                          # nada sonando: oculto
    (None, {"hide_when_idle": False}, True),
    (status(song()), {}, True),
    (status(song(), paused=True), {}, True),
    (status(song(), paused=True), {"hide_when_paused": True}, False),
    (status(song(), backup=True), {"show_backup": False}, False),
])
def test_reglas_de_visibilidad(st, cfg, visible):
    assert ov.state_message(st, OverlayConfig(**cfg))["visible"] is visible


def test_la_vista_previa_siempre_se_ve_y_trae_ejemplo():
    msg = ov.state_message(None, OverlayConfig(), preview=True)
    assert msg["visible"] is True and msg["song"]["title"] == ov.SAMPLE_SONG["title"]


def test_sanitize_corrige_valores_invalidos():
    cfg = OverlayConfig(width=99999, accent="red; } body{", font='Inter"; }', animation="boom",
                        bg_opacity="x", custom_css="a" * 30000, header_text="h" * 100, port=80)
    ov.sanitize(cfg)
    assert cfg.width == 1200 and cfg.accent == "#9146ff" and cfg.font == "Inter"
    assert cfg.animation == "slide" and cfg.bg_opacity == 85 and cfg.port == 1024
    assert len(cfg.custom_css) == ov.MAX_CSS and len(cfg.header_text) == ov.MAX_HEADER


def test_mensaje_de_estilo():
    msg = ov.config_message(OverlayConfig(background="#000000", bg_opacity=50, width=500, show_time=False))
    assert msg["vars"]["--bg"] == "rgba(0, 0, 0, 0.50)" and msg["vars"]["--w"] == "500px"
    assert msg["flags"]["show_time"] is False and msg["flags"]["show_cover"] is True


def test_progreso_se_recorta_a_la_duracion():
    assert ov.progress_message(250, 200) == {"type": "progress", "pos": 200.0, "dur": 200.0}
    assert ov.progress_message(-3, 0)["pos"] == 0.0


# ------------------------------------------------------------------ servidor
async def _receive(ws, n: int) -> list[dict]:
    return [json.loads((await asyncio.wait_for(ws.receive(), 3)).data) for _ in range(n)]


def test_servidor_pagina_websocket_y_token():
    async def main():
        srv = OverlayServer(OverlayConfig(port=free_port()), token="secreto")
        assert await srv.start() and srv.running
        base = f"http://127.0.0.1:{srv.port}"
        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(f"{base}/overlay/now-playing?t=otro") as r:
                    assert r.status == 403
                async with http.get(srv.url) as r:
                    assert r.status == 200 and "Sonando ahora" in await r.text()
                async with http.get(f"{base}/overlay/fonts/Inter-400.ttf") as r:
                    assert r.status == 200
                async with http.get(f"{base}/overlay/fonts/..%2F..%2Fconfig.py") as r:
                    assert r.status == 404
                async with http.ws_connect(f"{base}/overlay/ws?t=secreto") as ws:
                    cfg, state, prog = await _receive(ws, 3)
                    assert (cfg["type"], state["type"], prog["type"]) == ("config", "state", "progress")
                    assert state["visible"] is False and state["song"] is None
                    srv.set_status(status(song()))
                    (state,) = await _receive(ws, 1)
                    assert state["visible"] is True and state["song"]["requester"] == "ana"
                    srv.set_progress(30, 200)
                    assert (await _receive(ws, 1))[0] == {"type": "progress", "pos": 30.0, "dur": 200.0}
                    srv.config.accent = "#ff0000"
                    srv.config_changed()
                    cfg, _state = await _receive(ws, 2)
                    assert cfg["vars"]["--accent"] == "#ff0000"
        finally:
            await srv.stop()
        assert not srv.running

    asyncio.run(main())


def test_vista_previa_sin_cancion_no_recibe_el_progreso_real():
    async def main():
        srv = OverlayServer(OverlayConfig(port=free_port()), token="t")
        await srv.start()
        try:
            async with aiohttp.ClientSession() as http, \
                    http.ws_connect(f"http://127.0.0.1:{srv.port}/overlay/ws?t=t&preview=1") as ws:
                _cfg, state, prog = await _receive(ws, 3)
                assert state["song"]["title"] == ov.SAMPLE_SONG["title"] and prog["pos"] == 83.0
                srv.set_progress(5, 10)
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(ws.receive(), 0.3)
        finally:
            await srv.stop()

    asyncio.run(main())


def test_desactivado_no_escucha():
    async def main():
        srv = OverlayServer(OverlayConfig(enabled=False, port=free_port()))
        assert await srv.start() is False and not srv.running and srv.last_error == ""

    asyncio.run(main())


def test_puerto_ocupado_avisa():
    async def main():
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            srv = OverlayServer(OverlayConfig(port=busy.getsockname()[1]))
            assert await srv.start() is False
            assert "ocupado" in srv.last_error and not srv.running

    asyncio.run(main())


# ------------------------------------------------------------------ editor
class StubBot:
    def __init__(self):
        from alpekabot.config import AppConfig
        self.config = AppConfig()
        self.config.overlay.enabled = False
        self.overlay = OverlayServer(self.config.overlay, token="t")
        self.saved = 0

    def save_overlay_style(self):
        self.saved += 1

    async def set_overlay(self, enabled, port=None):
        return False


def test_editor_cambia_la_config_y_restablece():
    from alpekabot.ui.overlay_dialog import OverlayDialog

    bot = StubBot()
    dlg = OverlayDialog(bot)
    cfg = bot.config.overlay
    dlg._toggles["show_cover"].click()
    dlg._sliders["width"][0].setValue(700)
    dlg.header_text.setText("Escuchando")
    assert (cfg.show_cover, cfg.width, cfg.header_text) == (False, 700, "Escuchando")
    assert "Desactivado" in dlg._status.text() and not dlg._copy.isEnabled()
    dlg._reset()
    assert (cfg.show_cover, cfg.width, cfg.header_text) == (True, 460, "Sonando ahora")
    assert cfg.enabled is False and bot.saved >= 1          # restablecer no reactiva el overlay
    dlg.done(0)
