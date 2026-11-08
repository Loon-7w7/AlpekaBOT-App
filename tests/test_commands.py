import asyncio

import pytest
from conftest import BOT_ID, FakeClock, msg

from alpekabot.core.models import Permission, Platform
from alpekabot.features.commands import Command, CommandRepository, CommandService, normalize_name


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def repo(db, bot):
    return CommandRepository(db, bot.bus)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def service(bot, repo, clock):
    return CommandService(bot, repo, clock=clock)


# ------------------------------------------------------------ repositorio
def test_normalize_name():
    assert normalize_name("!Hola") == "hola"
    for bad in ["", "!", "con espacio", "a" * 33, "addcmd"]:
        with pytest.raises(ValueError):
            normalize_name(bad)


def test_crud(repo):
    c = repo.add(Command(name="!Hola", response="hola $user"))
    assert c.id and repo.get("hola").response == "hola $user"
    with pytest.raises(ValueError, match="Ya existe"):
        repo.add(Command(name="HOLA", response="x"))
    repo.update(Command(id=c.id, name="hola", response="nuevo", cooldown_s=0))
    assert repo.get("hola").response == "nuevo"
    repo.delete(c.id)
    assert repo.get("hola") is None


def test_validaciones(repo):
    with pytest.raises(ValueError):
        repo.add(Command(name="x", response="   "))
    with pytest.raises(ValueError):
        repo.add(Command(name="x", response="ok", platforms=frozenset()))


# ---------------------------------------------------------------- ejecución
def test_responde_y_cuenta_usos(service, repo, bot):
    repo.add(Command(name="hola", response="Hola $user, uso $count", cooldown_s=0))
    assert run(service.handle(msg("!hola"))) == "Hola pepe, uso 1"
    assert run(service.handle(msg("!HOLA extra"))) == "Hola pepe, uso 2"
    assert bot.sent[-1] == (Platform.TWITCH, "Hola pepe, uso 2")


def test_ignora_comandos_inexistentes_y_al_propio_bot(service, repo, bot):
    repo.add(Command(name="hola", response="hola", cooldown_s=0))
    assert run(service.handle(msg("!nada"))) is None
    assert run(service.handle(msg("!hola", user_id=BOT_ID))) is None
    assert bot.sent == []


def test_permisos(service, repo):
    repo.add(Command(name="mod", response="ok", permission=Permission.MODERATOR, cooldown_s=0))
    assert run(service.handle(msg("!mod"))) is None
    assert run(service.handle(msg("!mod", badges=("vip",)))) is None
    assert run(service.handle(msg("!mod", badges=("moderator",)))) == "ok"
    assert run(service.handle(msg("!mod", badges=("broadcaster",)))) == "ok"


def test_niveles_de_permiso():
    assert Permission.of_badges(("founder",)) is Permission.SUBSCRIBER
    assert Permission.of_badges(("subscriber", "vip")) is Permission.VIP
    assert Permission.of_badges(()) is Permission.EVERYONE


def test_cooldown_global(service, repo, clock):
    repo.add(Command(name="hola", response="ok", cooldown_s=30))
    assert run(service.handle(msg("!hola"))) == "ok"
    clock.advance(10)
    assert run(service.handle(msg("!hola", user="otro"))) is None  # global: afecta a todos
    clock.advance(21)
    assert run(service.handle(msg("!hola"))) == "ok"


def test_desactivado_y_plataforma(service, repo):
    repo.add(Command(name="off", response="x", enabled=False, cooldown_s=0))
    repo.add(Command(name="kick", response="x", platforms=frozenset({Platform.KICK}), cooldown_s=0))
    assert run(service.handle(msg("!off"))) is None
    assert run(service.handle(msg("!kick"))) is None


def test_respuesta_truncada(service, repo):
    repo.add(Command(name="largo", response="a" * 600, cooldown_s=0))
    assert len(run(service.handle(msg("!largo")))) == 500


# ---------------------------------------------------- comandos de gestión
MOD = ("moderator",)


def test_addcmd_editcmd_delcmd(service, repo):
    assert run(service.handle(msg("!addcmd !saludo Hola $user!", badges=MOD))) == "@pepe Comando !saludo creado."
    assert repo.get("saludo").response == "Hola $user!"
    assert "Ya existe" in run(service.handle(msg("!addcmd !saludo otra", badges=MOD)))
    assert run(service.handle(msg("!editcmd saludo Adiós", badges=MOD))) == "@pepe Comando !saludo actualizado."
    assert repo.get("saludo").response == "Adiós"
    assert run(service.handle(msg("!delcmd !saludo", badges=MOD))) == "@pepe Comando !saludo eliminado."
    assert repo.get("saludo") is None


def test_gestion_solo_mods(service, repo):
    assert run(service.handle(msg("!addcmd !x hola"))) is None
    assert repo.get("x") is None


def test_gestion_uso_incorrecto(service):
    assert "Uso:" in run(service.handle(msg("!addcmd !solo", badges=MOD)))
    assert "Uso:" in run(service.handle(msg("!delcmd", badges=MOD)))
    assert "No existe" in run(service.handle(msg("!delcmd !fantasma", badges=MOD)))
