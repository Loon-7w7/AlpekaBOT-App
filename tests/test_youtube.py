import asyncio
import json

import pytest
from aiohttp import web

from alpekabot.features import youtube
from alpekabot.features.youtube import YouTubeClient, YouTubeError, format_duration, parse_duration, parse_video_id

VID = "dQw4w9WgXcQ"


@pytest.mark.parametrize("text", [
    f"https://www.youtube.com/watch?v={VID}",
    f"https://youtube.com/watch?v={VID}&t=42s&list=PL123",
    f"https://youtu.be/{VID}?si=abc",
    f"youtu.be/{VID}",
    f"https://m.youtube.com/watch?v={VID}",
    f"https://music.youtube.com/watch?v={VID}",
    f"https://www.youtube.com/shorts/{VID}",
    f"https://www.youtube.com/embed/{VID}",
    f"https://www.youtube.com/live/{VID}",
])
def test_parse_video_id(text):
    assert parse_video_id(text) == VID


@pytest.mark.parametrize("text", [
    "never gonna give you up", VID, "thunderbolts", "https://evil.com/watch?v=" + VID,
    "https://youtube.com.evil.com/watch?v=" + VID, "https://www.youtube.com/watch?v=corto",
])
def test_parse_video_id_rechaza(text):
    assert parse_video_id(text) is None


def test_duraciones():
    assert parse_duration("PT3M33S") == 213
    assert parse_duration("PT1H2M3S") == 3723
    assert parse_duration("P1DT1S") == 86401
    assert parse_duration("") == 0
    assert format_duration(213) == "3:33"
    assert format_duration(3723) == "1:02:03"


# ------------------------------------------- cliente contra un servidor falso
SECRET_KEY = "CLAVE-SUPER-SECRETA-123"


async def _with_server(handler, coro_fn):
    app = web.Application()
    app.router.add_get("/{endpoint}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    old = youtube.API_BASE
    youtube.API_BASE = f"http://127.0.0.1:{port}/"
    try:
        return await coro_fn()
    finally:
        youtube.API_BASE = old
        await runner.cleanup()


def _error(status, reason):
    async def handler(request):
        assert request.query["key"] == SECRET_KEY
        return web.json_response({"error": {"errors": [{"reason": reason}]}}, status=status)
    return handler


@pytest.mark.parametrize("status,reason,expected", [
    (403, "quotaExceeded", "cuota"),
    (400, "keyInvalid", "no es válida"),
    (500, "backendError", "YouTube devolvió un error"),
])
def test_errores_de_api_sin_filtrar_la_clave(status, reason, expected, caplog):
    client = YouTubeClient(lambda: SECRET_KEY)
    with pytest.raises(YouTubeError) as err:
        asyncio.run(_with_server(_error(status, reason), lambda: client.get_video(VID)))
    assert expected in str(err.value)
    assert SECRET_KEY not in str(err.value)
    assert SECRET_KEY not in caplog.text


def test_sin_clave():
    with pytest.raises(YouTubeError, match="Falta la clave"):
        asyncio.run(YouTubeClient(lambda: "").get_video(VID))


def test_error_de_red_sin_filtrar_la_clave(caplog):
    old = youtube.API_BASE
    youtube.API_BASE = "http://127.0.0.1:1/"  # puerto cerrado
    try:
        with pytest.raises(YouTubeError) as err:
            asyncio.run(YouTubeClient(lambda: SECRET_KEY).get_video(VID))
    finally:
        youtube.API_BASE = old
    assert SECRET_KEY not in str(err.value) and err.value.__cause__ is None
    assert SECRET_KEY not in caplog.text


def test_busqueda_salta_directos_y_acepta_no_incrustables():
    async def handler(request):
        ep = request.match_info["endpoint"]
        if ep == "search":
            ids = ["live0000000", "noembed0000", "ok000000000"]
            return web.json_response({"items": [{"id": {"videoId": i}} for i in ids]})
        items = [
            {"id": "live0000000", "snippet": {"title": "Directo", "liveBroadcastContent": "live"},
             "contentDetails": {"duration": "P0D"}, "status": {"embeddable": True}},
            {"id": "noembed0000", "snippet": {"title": "No", "liveBroadcastContent": "none"},
             "contentDetails": {"duration": "PT3M"}, "status": {"embeddable": False}},
            {"id": "ok000000000", "snippet": {"title": "Buena", "channelTitle": "Canal",
                                              "liveBroadcastContent": "none"},
             "contentDetails": {"duration": "PT3M5S"}, "status": {"embeddable": True}},
        ]
        return web.Response(text=json.dumps({"items": items}), content_type="application/json")

    client = YouTubeClient(lambda: SECRET_KEY)
    v = asyncio.run(_with_server(handler, lambda: client.search("algo")))
    assert v.video_id == "noembed0000" and v.duration_s == 180
