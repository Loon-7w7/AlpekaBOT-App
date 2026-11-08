import asyncio

import pytest

from alpekabot.core.models import Platform, StreamInfo
from alpekabot.features.variables import RenderContext, read_api, render


def run(coro):
    return asyncio.run(coro)


def ctx(**kw):
    base = dict(platform=Platform.TWITCH, channel="MiCanal", user_name="Pepe", args=[], count=3,
                stream=StreamInfo(Platform.TWITCH, True, "Mi título", "Minecraft"))
    base.update(kw)
    return RenderContext(**base)


async def no_fetch(url):
    raise AssertionError(f"no debería llamar a readapi ({url})")


def test_variables_basicas():
    out = run(render("Hola $user en $channel ($count) $platform $game $title", ctx(), no_fetch))
    assert out == "Hola Pepe en MiCanal (3) Twitch Minecraft Mi título"


def test_target_usa_argumento_o_el_propio_usuario():
    assert run(render("$target", ctx(args=["@Ana"]), no_fetch)) == "Ana"
    assert run(render("$target", ctx(args=[]), no_fetch)) == "Pepe"


def test_lo_que_escribe_el_usuario_no_se_interpreta():
    # Un viewer no debe poder ejecutar $readapi ni otras variables a través de $args
    out = run(render("Dijiste: $args", ctx(args=["$readapi(http://127.0.0.1)", "$user"]), no_fetch))
    assert out == "Dijiste: $readapi(http://127.0.0.1) $user"


def test_random_y_pick():
    for _ in range(50):
        assert 1 <= int(run(render("$random(1,6)", ctx(), no_fetch))) <= 6
        assert run(render("$pick(a|b|c)", ctx(), no_fetch)) in {"a", "b", "c"}
    assert run(render("$random(5,1)", ctx(), no_fetch)) in {str(i) for i in range(1, 6)}


def test_random_invalido_queda_igual():
    assert run(render("$random(x,y)", ctx(), no_fetch)) == "$random(x,y)"


def test_uptime_offline():
    assert run(render("$uptime", ctx(stream=StreamInfo(Platform.TWITCH)), no_fetch)) == "offline"


def test_readapi_limite_por_mensaje():
    calls = []

    async def fake(url):
        calls.append(url)
        return "ok"

    out = run(render(" ".join(["$readapi(https://e.com)"] * 4), ctx(), fake))
    assert len(calls) == 3
    assert out.endswith("[readapi: demasiadas llamadas]")


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1/", "http://localhost:4455/", "http://192.168.1.1/", "http://10.0.0.5/",
     "http://[::1]/", "http://169.254.169.254/latest/meta-data"],
)
def test_readapi_bloquea_red_local(url):
    assert run(read_api(url)) == "[readapi: dirección no permitida]"


@pytest.mark.parametrize("url", ["file:///C:/Windows/win.ini", "ftp://e.com", "no-es-url"])
def test_readapi_rechaza_esquemas(url):
    assert run(read_api(url)) == "[readapi: URL no válida]"
