"""!title, !game, la página «Directo» y las llamadas de Twitch/Kick para cambiar título y categoría."""

from __future__ import annotations

import asyncio

import aiohttp
import aiohttp.web
import pytest
from conftest import FakeBot, FakeClient, msg

from alpekabot.core.models import AccountRole, Category, ConnectionState, Platform, StreamInfo
from alpekabot.features import commands as commands_module
from alpekabot.features.commands import CommandRepository, CommandService
from alpekabot.features.stream_control import StreamControl, best_category
from alpekabot.platforms.base import PlatformError

MOD = ("moderator",)


def run(coro):
    return asyncio.run(coro)


class StreamClient(FakeClient):
    """FakeClient que además busca categorías y cambia título/categoría."""

    def __post_init__(self):
        super().__post_init__()
        self.catalog = [Category(self.platform, "1", "Minecraft Dungeons"), Category(self.platform, "2", "Minecraft")]
        self.updates: list[tuple[str | None, Category | None]] = []
        self.fail: str | None = None

    @property
    def state(self):
        return ConnectionState.CONNECTED if self.is_connected else ConnectionState.DISCONNECTED

    async def search_categories(self, query, limit=10):
        return [c for c in self.catalog if query.lower() in c.name.lower()][:limit]

    async def update_stream(self, title=None, category=None):
        if self.fail:
            raise PlatformError(self.fail)
        self.updates.append((title, category))
        info = self.stream_info
        self.stream_info = StreamInfo(self.platform, info.is_live, title or info.title,
                                      category.name if category else info.game)


@pytest.fixture(autouse=True)
def free_reserved_names():
    before = set(commands_module.RESERVED_NAMES)
    yield
    commands_module.RESERVED_NAMES.clear()
    commands_module.RESERVED_NAMES.update(before)


@pytest.fixture
def env(db):
    bot = FakeBot()
    bot.platforms[Platform.TWITCH] = StreamClient(Platform.TWITCH)
    bot.platforms[Platform.KICK] = StreamClient(Platform.KICK)
    bot.stream = StreamControl(bot.platforms)
    service = CommandService(bot, CommandRepository(db, bot.bus))
    bot.stream.register_commands(service)
    return bot, service


def handle(service, text, badges=(), platform=Platform.TWITCH):
    return run(service.handle(msg(text, badges=badges, platform=platform)))


# ---------------------------------------------------------------- lógica
def test_best_category_prefiere_la_exacta_y_si_no_la_primera():
    found = [Category(Platform.TWITCH, "1", "Minecraft Dungeons"), Category(Platform.TWITCH, "2", "MINECRAFT")]
    assert best_category("minecraft", found).id == "2"
    assert best_category("mine", found).id == "1"
    assert best_category("x", []) is None


def test_ver_titulo_y_categoria_cualquiera(env):
    bot, service = env
    assert handle(service, "!title") == "Título en Twitch: Título"
    assert handle(service, "!game", platform=Platform.KICK) == "Categoría en Kick: Juego"
    assert handle(service, "!title kick") == "Título en Kick: Título"


def test_un_viewer_no_puede_cambiar(env):
    bot, service = env
    assert handle(service, "!title Nuevo título") is None
    assert bot.platforms[Platform.TWITCH].updates == []


def test_mod_cambia_el_titulo_en_las_dos(env):
    bot, service = env
    reply = handle(service, "!title Jugando con   viewers", MOD)
    assert reply == "Título cambiado en Twitch y Kick."
    for p in Platform:
        assert bot.platforms[p].updates == [("Jugando con viewers", None)]
    assert bot.sent == [(Platform.TWITCH, reply)]


def test_prefijo_cambia_solo_una_plataforma(env):
    bot, service = env
    assert handle(service, "!title TWITCH Solo aquí", MOD) == "Título cambiado en Twitch."
    assert bot.platforms[Platform.KICK].updates == []


def test_prefijo_de_plataforma_desconectada(env):
    bot, service = env
    bot.platforms[Platform.KICK].is_connected = False
    assert handle(service, "!game kick Minecraft", MOD) == "Kick no está conectado."
    # sin prefijo: solo las conectadas
    assert handle(service, "!game minecraft", MOD) == "Categoría: Minecraft (Twitch)."


def test_game_usa_la_exacta_o_la_primera(env):
    bot, service = env
    assert handle(service, "!game minecraft", MOD) == "Categoría: Minecraft (Twitch y Kick)."
    assert handle(service, "!game twitch dungeons", MOD) == "Categoría: Minecraft Dungeons (Twitch)."
    assert bot.platforms[Platform.TWITCH].updates[-1][1].id == "1"


def test_game_sin_resultados_y_fallo_parcial(env):
    bot, service = env
    assert handle(service, "!game zzz", MOD).startswith("No se pudo cambiar.")
    bot.platforms[Platform.KICK].fail = "Kick rechazó el cambio"
    reply = handle(service, "!title Hola", MOD)
    assert reply == "Título cambiado en Twitch. No se pudo en Kick: Kick rechazó el cambio"


def test_desactivar_title_lo_silencia(env):
    bot, service = env
    service.set_enabled("title", False, save=False)
    assert handle(service, "!title Hola", MOD) is None


# ------------------------------------------------------------------ Twitch
class _AsyncList:
    def __init__(self, items):
        self._items = items

    def __aiter__(self):
        async def gen():
            for i in self._items:
                yield i
        return gen()


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _twitch_client():
    from alpekabot.core.event_bus import EventBus
    from alpekabot.core.models import AccountInfo
    from alpekabot.platforms.twitch.client import TwitchClient

    class FakeApi:
        def __init__(self):
            self.modified = []

        def get_games(self, names):
            return _AsyncList([_Obj(id="27471", name="Minecraft")] if names == ["minecraft"] else [])

        def search_categories(self, query, first=20):
            return _AsyncList([_Obj(id="9", name="Minecraft Dungeons"), _Obj(id="27471", name="Minecraft")])

        async def modify_channel_information(self, broadcaster_id, game_id=None, title=None):
            self.modified.append((broadcaster_id, game_id, title))
            return True

    c = TwitchClient(None, EventBus())
    c._streamer_api = FakeApi()
    c.streamer = AccountInfo(Platform.TWITCH, AccountRole.STREAMER, "100", "loon_vt", "loon_vt")
    c._state = ConnectionState.CONNECTED

    async def no_poll():
        return None
    c._poll_stream = no_poll
    return c


def test_twitch_busca_exacta_primero_sin_repetir():
    c = _twitch_client()
    found = run(c.search_categories("minecraft"))
    assert [x.id for x in found] == ["27471", "9"]


def test_twitch_update_stream():
    from alpekabot.platforms.twitch.client import MAX_TITLE_LEN
    c = _twitch_client()
    run(c.update_stream(title=" Hola ", category=Category(Platform.TWITCH, "27471", "Minecraft")))
    assert c._streamer_api.modified == [("100", "27471", "Hola")]
    with pytest.raises(PlatformError):
        run(c.update_stream(title="x" * (MAX_TITLE_LEN + 1)))
    with pytest.raises(PlatformError):
        run(c.update_stream(title="   "))


def test_scopes_nuevos():
    from twitchAPI.type import AuthScope

    from alpekabot.platforms.kick import auth as kick_auth
    from alpekabot.platforms.twitch import auth as twitch_auth
    assert AuthScope.CHANNEL_MANAGE_BROADCAST in twitch_auth.SCOPES[AccountRole.STREAMER]
    assert "channel:write" in kick_auth.SCOPES[AccountRole.STREAMER]


# -------------------------------------------------------------------- Kick
def test_kick_api_busca_v1_y_cae_a_v2_y_actualiza(monkeypatch):
    from alpekabot.platforms.kick import api as kick_api

    rec = {"v1": 0, "v2": [], "patch": []}

    async def h_v1(r):
        rec["v1"] += 1
        return aiohttp.web.json_response({"message": "gone"}, status=404)

    async def h_v2(r):
        rec["v2"].append(r.query.get("name"))
        return aiohttp.web.json_response({"data": [{"id": 5, "name": "Just Chatting"}], "message": "",
                                          "pagination": {"next_cursor": ""}})

    async def h_patch(r):
        rec["patch"].append((r.headers["Authorization"], await r.json()))
        return aiohttp.web.Response(status=204)

    app = aiohttp.web.Application()
    app.router.add_get("/public/v1/categories", h_v1)
    app.router.add_get("/public/v2/categories", h_v2)
    app.router.add_patch("/public/v1/channels", h_patch)

    async def main():
        runner = aiohttp.web.AppRunner(app)
        await runner.setup()
        await aiohttp.web.TCPSite(runner, "127.0.0.1", 0).start()
        monkeypatch.setattr(kick_api, "API_BASE", f"http://127.0.0.1:{runner.addresses[0][1]}/public/v1/")
        api = kick_api.KickAPI()
        try:
            found = await api.search_categories("tok", "just chatting")
            await api.update_channel("tok", title="Hola", category_id="5")
            await api.update_channel("tok")  # nada que cambiar: no llama
        finally:
            await api.close()
            await runner.cleanup()
        return found

    found = run(main())
    assert found == [{"id": 5, "name": "Just Chatting"}]
    assert rec["v1"] == 1 and rec["v2"] == ["just chatting"]
    assert rec["patch"] == [("Bearer tok", {"stream_title": "Hola", "category_id": 5})]


def _named(name):
    async def fn(*a):
        return None
    fn.__name__ = name
    return fn


def test_kick_client_ordena_la_exacta_primero():
    from alpekabot.core.event_bus import EventBus
    from alpekabot.core.models import AccountInfo
    from alpekabot.platforms.kick.client import KickClient

    c = KickClient.__new__(KickClient)
    c._bus = EventBus()
    c._state = ConnectionState.CONNECTED
    c.stream_info = StreamInfo(Platform.KICK)
    c.streamer = AccountInfo(Platform.KICK, AccountRole.STREAMER, "100", "loonvt", "LoonVT")
    calls = []

    async def fake_call(role, fn, *args):
        calls.append((role, fn.__name__, args))
        if fn.__name__ == "search_categories":
            return [{"id": 1, "name": "Minecraft Dungeons"}, {"id": 2, "name": "Minecraft"}]
        return None

    async def no_refresh():
        return None
    c._call = fake_call
    c._refresh_stream = no_refresh
    c._api = _Obj(search_categories=_named("search_categories"), update_channel=_named("update_channel"))
    assert [x.id for x in run(c.search_categories("minecraft"))] == ["2", "1"]
    run(c.update_stream(title="Hola", category=Category(Platform.KICK, "2", "Minecraft")))
    assert calls[-1] == (AccountRole.STREAMER, "update_channel", ("Hola", "2"))


# --------------------------------------------------------------------- UI
def test_pagina_directo_aplica_y_muestra(env):
    from alpekabot.ui.stream_tab import StreamTab
    bot, _service = env
    page = StreamTab(bot)
    assert page._status[Platform.TWITCH].title_label.full_text() == "Título"
    run(page.apply_title("Nuevo", [Platform.TWITCH]))
    assert bot.platforms[Platform.TWITCH].updates == [("Nuevo", None)]
    assert page._result.text() == "Título cambiado en Twitch."
    page.refresh()
    assert page._status[Platform.TWITCH].title_label.full_text() == "Nuevo"

    bot.platforms[Platform.KICK].fail = "sin permiso"
    run(page.apply_category("minecraft", [Platform.TWITCH, Platform.KICK]))
    assert page._result.text() == "Categoría: Minecraft (Twitch).\nKick: sin permiso"
    assert page._game_edit.text() == "Minecraft"


def test_pagina_directo_sin_conexion_desactiva(env):
    from alpekabot.ui.stream_tab import StreamTab
    bot, _service = env
    for c in bot.platforms.values():
        c.is_connected = False
    page = StreamTab(bot)
    assert not page._apply_title.isEnabled()
    page._title_edit.setText("x")
    page._on_apply_title()
    assert page._result.text() == "Elige al menos una plataforma conectada."
