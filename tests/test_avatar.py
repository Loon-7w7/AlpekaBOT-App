"""Foto de perfil del streamer en la barra lateral (en lugar de las iniciales)."""

import asyncio
import tempfile
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap

from alpekabot import bot as bot_module
from alpekabot.bot import AlpekaBot
from alpekabot.config import AppConfig
from alpekabot.core.models import Platform
from alpekabot.ui.shell import Sidebar


def _png(tmp_path) -> Path:
    pix = QPixmap(200, 120)
    pix.fill(Qt.red)
    path = tmp_path / "foto.img"
    pix.save(str(path), "PNG")
    return path


def test_la_barra_lateral_muestra_la_foto_y_vuelve_a_las_iniciales(qt_app, tmp_path):
    side = Sidebar()
    side.set_user("loon_vt")
    side.set_avatar(_png(tmp_path))
    avatar = side._avatar
    assert avatar.text() == "" and avatar.pixmap().toImage().pixelColor(20, 20) == Qt.red
    side.set_user("otro_nombre")  # con foto, cambiar el nombre no pone iniciales encima
    assert avatar.text() == ""
    side.set_avatar(None)
    assert avatar.pixmap().isNull() and avatar.text() == "ON"
    side.set_avatar(tmp_path / "no-existe.img")  # archivo roto o ausente: iniciales
    assert avatar.text() == "ON"


def test_sin_datos_reales_no_se_descarga_ni_se_lee_foto(qt_app, tmp_path):
    bot = AlpekaBot(AppConfig(), db_path=Path(tempfile.mkdtemp()) / "t.db")  # modo prueba
    assert bot.cached_avatar(Platform.TWITCH) is None
    assert asyncio.run(bot.fetch_avatar(Platform.TWITCH)) is None


def test_solo_se_descargan_fotos_https(qt_app, tmp_path, monkeypatch):
    monkeypatch.setattr(bot_module, "DATA_DIR", tmp_path)
    bot = AlpekaBot(AppConfig(), db_path=Path(tempfile.mkdtemp()) / "t.db")
    bot._real_data = True
    client = bot.platforms[Platform.TWITCH]
    monkeypatch.setattr(type(client), "is_connected", property(lambda self: True))

    async def http_url():
        return "http://ejemplo.invalid/foto.png"

    monkeypatch.setattr(client, "profile_image_url", http_url)
    assert asyncio.run(bot.fetch_avatar(Platform.TWITCH)) is None
    assert not (tmp_path / "avatars").exists()


def test_la_imagen_se_descarga_entera_aunque_llegue_por_trozos(qt_app):
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    image = bytes(range(256)) * 2000  # 512 KB, varios trozos

    async def handler(request):
        response = web.StreamResponse(headers={"Content-Type": "image/png"})
        await response.prepare(request)
        for i in range(0, len(image), 30_000):
            await response.write(image[i:i + 30_000])
            await asyncio.sleep(0)
        return response

    async def too_big(request):
        return web.Response(body=b"x" * (bot_module.AVATAR_MAX_BYTES + 1), content_type="image/png")

    async def not_image(request):
        return web.Response(body=b"hola", content_type="text/html")

    async def main():
        app = web.Application()
        app.router.add_get("/ok", handler)
        app.router.add_get("/big", too_big)
        app.router.add_get("/html", not_image)
        async with TestServer(app) as server:
            base = str(server.make_url("/")).rstrip("/")
            return (await bot_module.download_image(base + "/ok"), await bot_module.download_image(base + "/big"),
                    await bot_module.download_image(base + "/html"))

    ok, big, html = asyncio.run(main())
    assert ok == image and big is None and html is None
