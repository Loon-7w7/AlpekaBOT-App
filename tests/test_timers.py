import asyncio

import pytest
from conftest import BOT_ID, FakeClock, msg

from alpekabot.core.models import Platform, StreamInfo
from alpekabot.features.timers import Timer, TimerRepository, TimerService


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def repo(db, bot):
    return TimerRepository(db, bot.bus)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def service(bot, repo, clock):
    return TimerService(bot, repo, clock=clock)


def chat(bot, n, **kw):
    for _ in range(n):
        bot.bus.chat_message.emit(msg("hola", **kw))


def test_envia_tras_intervalo_y_mensajes(service, repo, bot, clock):
    repo.add(Timer(name="redes", message="Sígueme en $channel", interval_min=10, min_lines=3))
    assert run(service.tick()) == []          # primera ronda: empieza a contar
    clock.advance(10 * 60)
    chat(bot, 2)
    assert run(service.tick()) == []          # faltan mensajes
    chat(bot, 1)
    assert run(service.tick()) == [(Platform.TWITCH, "Sígueme en MiCanal")]
    chat(bot, 5)
    assert run(service.tick()) == []          # se reinició el intervalo


def test_no_envia_fuera_de_directo_o_desconectado(service, repo, bot, clock):
    repo.add(Timer(name="t", message="x", interval_min=1, min_lines=0))
    client = bot.platforms[Platform.TWITCH]
    client.stream_info = StreamInfo(Platform.TWITCH, is_live=False)
    run(service.tick()); clock.advance(120)
    assert run(service.tick()) == []
    client.stream_info = StreamInfo(Platform.TWITCH, is_live=True)
    client.is_connected = False
    run(service.tick()); clock.advance(120)
    assert run(service.tick()) == []


def test_mensajes_del_bot_no_cuentan(service, repo, bot, clock):
    repo.add(Timer(name="t", message="x", interval_min=1, min_lines=2))
    run(service.tick())
    clock.advance(120)
    chat(bot, 5, user_id=BOT_ID)
    assert run(service.tick()) == []


def test_un_timer_por_plataforma_y_ronda(service, repo, bot, clock):
    repo.add(Timer(name="a", message="A", interval_min=1, min_lines=0))
    repo.add(Timer(name="b", message="B", interval_min=1, min_lines=0))
    run(service.tick())
    clock.advance(60)
    assert run(service.tick()) == [(Platform.TWITCH, "A")]
    assert run(service.tick()) == [(Platform.TWITCH, "B")]


def test_empezar_directo_reinicia_espera(service, repo, bot, clock):
    repo.add(Timer(name="t", message="x", interval_min=5, min_lines=0))
    run(service.tick())
    clock.advance(5 * 60)
    bot.bus.stream_status.emit(StreamInfo(Platform.TWITCH, is_live=True))  # acaba de empezar
    assert run(service.tick()) == []
    clock.advance(5 * 60)
    assert len(run(service.tick())) == 1


def test_desactivado(service, repo, clock):
    repo.add(Timer(name="t", message="x", interval_min=1, min_lines=0, enabled=False))
    run(service.tick()); clock.advance(600)
    assert run(service.tick()) == []


def test_validaciones(repo):
    for bad in [Timer(name="", message="x"), Timer(name="a", message=" "),
                Timer(name="a", message="x", interval_min=0), Timer(name="a", message="x", platforms=frozenset())]:
        with pytest.raises(ValueError):
            repo.add(bad)
