import asyncio
import json

import pytest
from aiohttp import web

from alpekabot.features import youtube
from alpekabot.features.youtube import (InnerTube, OfficialAPI, YouTubeClient, YouTubeError, format_duration,
                                       parse_clock, parse_duration, parse_video_id)

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
    client = OfficialAPI(lambda: SECRET_KEY)
    with pytest.raises(YouTubeError) as err:
        asyncio.run(_with_server(_error(status, reason), lambda: client.get_video(VID)))
    assert expected in str(err.value)
    assert SECRET_KEY not in str(err.value)
    assert SECRET_KEY not in caplog.text


def test_api_oficial_sin_clave():
    with pytest.raises(YouTubeError, match="Falta la clave"):
        asyncio.run(OfficialAPI(lambda: "").get_video(VID))


def test_error_de_red_sin_filtrar_la_clave(caplog):
    old = youtube.API_BASE
    youtube.API_BASE = "http://127.0.0.1:1/"  # puerto cerrado
    try:
        with pytest.raises(YouTubeError) as err:
            asyncio.run(OfficialAPI(lambda: SECRET_KEY).get_video(VID))
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

    client = OfficialAPI(lambda: SECRET_KEY)
    v = asyncio.run(_with_server(handler, lambda: client.search("algo")))
    assert v.video_id == "noembed0000" and v.duration_s == 180


def test_parse_clock():
    assert parse_clock("3:45") == 225
    assert parse_clock("6:10:58") == 22258
    assert parse_clock("") == 0
    assert parse_clock("EN DIRECTO") == 0


# ------------------------------------------------- InnerTube (sin clave)
def _player(vid, title="Canción", length="213", **extra):
    return {"playabilityStatus": {"status": "UNPLAYABLE"},
            "videoDetails": {"videoId": vid, "title": title, "author": "Canal", "channelId": "UC1",
                             "lengthSeconds": length, **extra}}


def _renderer(vid, length=None, live=False):
    vr = {"videoId": vid, "title": {"runs": [{"text": "Vídeo "}, {"text": vid}]},
          "ownerText": {"runs": [{"text": "Canal", "navigationEndpoint": {"browseEndpoint": {"browseId": "UC9"}}}]}}
    if length:
        vr["lengthText"] = {"simpleText": length}
    if live:
        vr["badges"] = [{"metadataBadgeRenderer": {"style": "BADGE_STYLE_TYPE_LIVE_NOW"}}]
    return {"videoRenderer": vr}


SEARCH_RESPONSE = {"contents": {"twoColumnSearchResultsRenderer": {"primaryContents": {"sectionListRenderer": {
    "contents": [
        {"itemSectionRenderer": {"contents": [
            {"adSlotRenderer": {}},
            _renderer("live0000000", live=True),
            _renderer("upcoming000"),          # sin duración: estreno o directo
            _renderer("ok000000000", "3:05"),
            _renderer("second00000", "4:00"),
        ]}},
    ]}}}}}


class FakeYouTube:
    """Servidor con las dos APIs: GET /{endpoint} (oficial) y POST /youtubei/v1/{endpoint}."""

    def __init__(self, official=None, player=None, search=None):
        self.official, self.player, self.search = official, player or {}, search or SEARCH_RESPONSE
        self.calls: list[str] = []

    async def _official(self, request):
        self.calls.append("official")
        return await self.official(request)

    async def _innertube(self, request):
        ep = request.match_info["endpoint"]
        body = await request.json()
        assert body["context"]["client"]["clientName"] == "WEB"
        self.calls.append(f"innertube:{ep}")
        if ep == "player":
            return web.json_response(self.player.get(body["videoId"], {"playabilityStatus": {"status": "ERROR"}}))
        assert body["query"]
        return web.json_response(self.search)

    async def run(self, coro_fn):
        app = web.Application()
        app.router.add_post("/youtubei/v1/{endpoint}", self._innertube)
        if self.official:
            app.router.add_get("/{endpoint}", self._official)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        base = f"http://127.0.0.1:{runner.addresses[0][1]}/"
        old = youtube.API_BASE, youtube.INNERTUBE_BASE
        youtube.API_BASE, youtube.INNERTUBE_BASE = base, base + "youtubei/v1/"
        try:
            return await coro_fn()
        finally:
            youtube.API_BASE, youtube.INNERTUBE_BASE = old
            await runner.cleanup()


def test_sin_clave_usa_innertube_para_enlaces():
    fake = FakeYouTube(player={VID: _player(VID, "Never Gonna", "213")})
    client = YouTubeClient(lambda: "")
    v = asyncio.run(fake.run(lambda: client.get_video(VID)))
    assert (v.video_id, v.title, v.channel, v.channel_id, v.duration_s, v.is_live) == \
        (VID, "Never Gonna", "Canal", "UC1", 213, False)
    assert fake.calls == ["innertube:player"]


def test_innertube_video_inexistente_y_directo():
    fake = FakeYouTube(player={"live0000000": _player("live0000000", length="0", isLive=True),
                               "estreno0000": _player("estreno0000", isUpcoming=True)})
    client = YouTubeClient(lambda: "")
    assert asyncio.run(fake.run(lambda: client.get_video("noexiste000"))) is None
    assert asyncio.run(fake.run(lambda: client.get_video("live0000000"))).is_live
    assert asyncio.run(fake.run(lambda: client.get_video("estreno0000"))).is_live


def test_innertube_busqueda_salta_directos_y_sin_duracion():
    fake = FakeYouTube()  # el player no conoce el vídeo: se usan los datos de la búsqueda
    v = asyncio.run(fake.run(lambda: YouTubeClient(lambda: "").search("algo")))
    assert (v.video_id, v.title, v.channel, v.channel_id, v.duration_s) == \
        ("ok000000000", "Vídeo ok000000000", "Canal", "UC9", 185)


def test_innertube_busqueda_completa_con_los_datos_del_video():
    # El canal de la búsqueda (artista) puede no ser el que sube el vídeo (VEVO)
    fake = FakeYouTube(player={"ok000000000": _player("ok000000000", "Título real", "190")})
    v = asyncio.run(fake.run(lambda: YouTubeClient(lambda: "").search("algo")))
    assert (v.video_id, v.title, v.channel_id, v.duration_s) == ("ok000000000", "Título real", "UC1", 190)
    assert fake.calls == ["innertube:search", "innertube:player"]


def test_innertube_busqueda_sin_resultados():
    fake = FakeYouTube(search={"contents": {}})
    assert asyncio.run(fake.run(lambda: YouTubeClient(lambda: "").search("algo"))) is None


def test_innertube_error_http():
    async def boom(request):
        return web.Response(status=500)

    async def go():
        app = web.Application()
        app.router.add_post("/youtubei/v1/{endpoint}", boom)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        old = youtube.INNERTUBE_BASE
        youtube.INNERTUBE_BASE = f"http://127.0.0.1:{runner.addresses[0][1]}/youtubei/v1/"
        try:
            return await InnerTube().get_video(VID)
        finally:
            youtube.INNERTUBE_BASE = old
            await runner.cleanup()

    with pytest.raises(YouTubeError, match="devolvió un error"):
        asyncio.run(go())


def test_con_clave_usa_la_oficial():
    async def ok(request):
        return web.json_response({"items": [{"id": VID, "snippet": {"title": "Oficial"},
                                             "contentDetails": {"duration": "PT1M"}, "status": {}}]})
    fake = FakeYouTube(official=ok)
    v = asyncio.run(fake.run(lambda: YouTubeClient(lambda: SECRET_KEY).get_video(VID)))
    assert v.title == "Oficial" and fake.calls == ["official"]


def test_cuota_agotada_pasa_a_innertube_y_pausa_la_oficial(caplog):
    now = [1000.0]
    fake = FakeYouTube(official=_error(403, "quotaExceeded"), player={VID: _player(VID, "Sin clave")})
    client = YouTubeClient(lambda: SECRET_KEY, clock=lambda: now[0])

    v = asyncio.run(fake.run(lambda: client.get_video(VID)))
    assert v.title == "Sin clave"
    assert fake.calls == ["official", "innertube:player"]
    assert SECRET_KEY not in caplog.text

    fake.calls.clear()
    asyncio.run(fake.run(lambda: client.get_video(VID)))
    assert fake.calls == ["innertube:player"]  # en pausa: ni se intenta la oficial

    now[0] += youtube.OFFICIAL_PAUSE_S
    fake.calls.clear()
    asyncio.run(fake.run(lambda: client.get_video(VID)))
    assert fake.calls == ["official", "innertube:player"]  # pasada la pausa, se vuelve a probar
