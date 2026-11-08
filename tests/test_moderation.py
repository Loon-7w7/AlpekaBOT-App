import asyncio
import itertools

import pytest
from conftest import BOT_ID, FakeBot, FakeClient, FakeClock

from alpekabot.config import ModerationConfig
from alpekabot.core.models import ChatMessage, Platform
from alpekabot.features.commands import Command, CommandRepository, CommandService
from alpekabot.features.moderation import (
    REASON_LINK,
    REASON_WORD,
    ModerationRepository,
    ModerationService,
    compile_word,
    find_links,
    fold,
    forbidden_link,
    format_duration,
    format_escalation,
    parse_duration,
    parse_escalation,
)
from alpekabot.platforms.base import PlatformError

WHITELIST = ModerationConfig().links_whitelist
MOD = ("moderator",)
_ids = itertools.count(1)


def run(coro):
    return asyncio.run(coro)


def chat(text, user="pepe", badges=(), user_id="u1", platform=Platform.TWITCH) -> ChatMessage:
    return ChatMessage(platform, f"m{next(_ids)}", "c", user_id, user.lower(), user, text, badges=badges)


class ModClient(FakeClient):
    """Cliente falso que apunta las acciones de moderación."""

    def __post_init__(self):
        super().__post_init__()
        self.calls = []
        self.fail = set()
        self.users = {}

    async def _do(self, name, *args):
        if name in self.fail:
            raise PlatformError(f"falla {name}")
        self.calls.append((name, *args))

    async def delete_message(self, message_id):
        await self._do("delete", message_id)

    async def timeout_user(self, user_id, seconds, reason=""):
        await self._do("timeout", user_id, seconds)

    async def ban_user(self, user_id, reason=""):
        await self._do("ban", user_id)

    async def unban_user(self, user_id):
        await self._do("unban", user_id)

    async def lookup_user(self, login):
        return self.users.get(login)


@pytest.fixture
def client():
    return ModClient(Platform.TWITCH)


@pytest.fixture
def bot(client):
    b = FakeBot()
    b.platforms[Platform.TWITCH] = client
    return b


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def config():
    return ModerationConfig()


@pytest.fixture
def svc(bot, db, config, clock):
    s = ModerationService(bot, ModerationRepository(db, bot.bus), config, clock=clock)
    bot.moderation = s
    return s


@pytest.fixture
def cmds(bot, svc, db):
    service = CommandService(bot, CommandRepository(db, bot.bus))
    svc.register_commands(service)
    return service


def feed(bot, svc, *messages):
    """Emite los mensajes en el bus (dentro de un bucle) y espera a las sanciones."""
    async def go():
        for m in messages:
            bot.bus.chat_message.emit(m)
        await svc.wait_idle()
    run(go())


def kinds(client):
    return [c[0] for c in client.calls]


# ------------------------------------------------------------- utilidades
def test_duraciones_y_escalado():
    assert parse_duration("600") == 600 and parse_duration("10m") == 600 and parse_duration("1h") == 3600
    assert parse_duration("2d") == 172_800 and parse_duration("abc") is None and parse_duration("0") is None
    assert format_duration(45) == "45 s" and format_duration(600) == "10 min"
    assert format_duration(5400) == "1 h 30 min"
    assert parse_escalation("aviso, 1m, 600, ban") == [0, 60, 600, -1]
    assert format_escalation([0, 60, -1]) == "aviso, 60, ban"
    for bad in ("", "aviso, nada", ",".join(["aviso"] * 11)):
        with pytest.raises(ValueError):
            parse_escalation(bad)


@pytest.mark.parametrize("text,hosts", [
    ("mira https://Evil.com/x?y=1", ["evil.com"]),
    ("entra en pagina.com/oferta ya", ["pagina.com"]),
    ("www.algo.net", ["www.algo.net"]),
    ("http://user:pw@malo.xyz:8080/a", ["malo.xyz"]),
    ("escríbeme a correo@gmail.com", []),
    ("versión 1.5 y hola.que tal", []),
    ("hola... adiós", []),
])
def test_detecta_enlaces(text, hosts):
    assert find_links(text) == hosts


def test_lista_blanca():
    assert forbidden_link("https://www.youtube.com/watch?v=x", WHITELIST) is None
    assert forbidden_link("clips.twitch.tv/abc y youtu.be/x", WHITELIST) is None
    assert forbidden_link("youtube.com.evil.xyz/login", WHITELIST) == "youtube.com.evil.xyz"
    assert forbidden_link("notyoutube.com", WHITELIST) == "notyoutube.com"


@pytest.mark.parametrize("text", [
    "PUTA", "eres un puuuta", "p.u.t.a", "p u t a", "pvt4", "pütá!", "putas", "hola,puta.",
])
def test_palabra_prohibida_detecta_trucos(text):
    assert compile_word("puta").search(fold(text))


@pytest.mark.parametrize("text", ["computadora", "disputa", "imputado", "nada que ver"])
def test_palabra_prohibida_sin_falsos_positivos(text):
    assert not compile_word("puta").search(fold(text))


def test_letras_dobles_no_se_confunden():
    assert not compile_word("perro").search(fold("pero no"))
    assert compile_word("perro").search(fold("PERRROS"))


def test_frases_regex_y_repositorio(db, bot):
    assert compile_word("hijo de puta").search(fold("HIJO DE PUTA"))
    assert compile_word(r"spam\d+", is_regex=True).search("compra spam123")
    with pytest.raises(ValueError):
        compile_word("(", is_regex=True)
    repo = ModerationRepository(db, bot.bus)
    repo.word_add("Pútá")
    with pytest.raises(ValueError, match="ya está"):
        repo.word_add("puta")
    assert [w.pattern for w in repo.words_list()] == ["puta"]
    assert len(repo.compiled_words()) == 1
    repo.word_remove(repo.words_list()[0].id)
    assert repo.compiled_words() == []


# ---------------------------------------------------------------- filtros
def test_escalado_aviso_timeout_y_olvido(bot, svc, client, clock):
    first = chat("visita malo.com")
    feed(bot, svc, first)
    assert client.calls == [("delete", first.message_id)]
    assert bot.sent[-1] == (Platform.TWITCH, f"@pepe {REASON_LINK} (aviso 1).")

    client.calls.clear()
    feed(bot, svc, chat("otra vez malo.com"))
    assert kinds(client) == ["delete", "timeout"] and client.calls[1] == ("timeout", "u1", 60)
    assert bot.sent[-1][1] == f"@pepe {REASON_LINK}: timeout de 1 min."

    feed(bot, svc, chat("y otra malo.com"), chat("y otra más malo.com"))
    assert [c for c in client.calls if c[0] == "timeout"][-2:] == [("timeout", "u1", 600)] * 2

    clock.advance(61 * 60)  # pasada la hora sin infracciones, se olvidan
    client.calls.clear()
    feed(bot, svc, chat("malo.com"))
    assert kinds(client) == ["delete"]
    assert svc.strikes_of(Platform.TWITCH, "u1") == 1


def test_escalado_con_ban(bot, svc, client, config):
    config.escalation = [0, -1]
    feed(bot, svc, chat("malo.com"), chat("malo.com"))
    assert kinds(client) == ["delete", "delete", "ban"]
    assert bot.sent[-1][1] == f"@pepe {REASON_LINK}: baneado."


def test_strikes_comunes_y_por_plataforma(bot, svc, client):
    svc.repo.word_add("tonto")
    feed(bot, svc, chat("malo.com"), chat("eres tonto"))
    assert ("timeout", "u1", 60) in client.calls  # el insulto fue la 2.ª infracción
    assert svc.strikes_of(Platform.KICK, "u1") == 0
    acts = svc.repo.recent_actions()
    assert [(a.action, a.reason) for a in acts] == [("timeout", REASON_WORD), ("warn", REASON_LINK)]
    assert acts[0].source == "auto" and acts[0].message == "eres tonto"


def test_exentos_por_filtro(bot, svc, client):
    svc.repo.word_add("tonto")
    feed(bot, svc, chat("mira malo.com", badges=("vip",)))      # VIP: puede poner enlaces
    assert client.calls == []
    feed(bot, svc, chat("tonto", badges=("vip",), user_id="v"))  # pero no insultar
    assert kinds(client) == ["delete"]
    client.calls.clear()
    feed(bot, svc, chat("tonto malo.com", badges=MOD), chat("tonto", badges=("broadcaster",)))
    feed(bot, svc, chat("tonto malo.com", user_id=BOT_ID))      # el propio bot
    assert client.calls == []


def test_filtros_desactivados(bot, svc, client, config):
    config.links_enabled = False
    feed(bot, svc, chat("malo.com"))
    config.links_enabled, config.enabled = True, False
    feed(bot, svc, chat("malo.com"))
    assert client.calls == []


def test_sin_aviso_en_el_chat(bot, svc, client, config):
    config.announce = False
    feed(bot, svc, chat("malo.com"))
    assert kinds(client) == ["delete"] and bot.sent == []


def test_permit_vale_para_un_enlace(bot, svc, client, clock):
    svc.permit(Platform.TWITCH, "@Pepe")
    feed(bot, svc, chat("mi web: pepe.com"))
    assert client.calls == []
    feed(bot, svc, chat("y otra: pepe.com"))
    assert kinds(client) == ["delete"]
    svc.permit(Platform.TWITCH, "pepe")
    clock.advance(61)  # caducado
    feed(bot, svc, chat("tarde: pepe.com"))
    assert kinds(client).count("delete") == 2


def test_enlace_permitido_no_gasta_el_permit(bot, svc, client):
    svc.permit(Platform.TWITCH, "pepe")
    feed(bot, svc, chat("https://youtu.be/abc"), chat("malo.com"))
    assert client.calls == []


def test_si_la_api_falla_no_se_registra(bot, svc, client):
    client.fail = {"delete"}
    feed(bot, svc, chat("malo.com"))
    assert svc.repo.recent_actions() == [] and bot.sent == []
    client.fail = {"timeout"}
    feed(bot, svc, chat("malo.com"))  # 2.ª: borra, el timeout falla → queda como aviso
    assert svc.repo.recent_actions()[0].action == "warn"


def test_mensaje_sancionado_no_ejecuta_comandos(bot, svc, client, db):
    commands = CommandRepository(db, bot.bus)
    commands.add(Command(name="hola", response="hola!", cooldown_s=0))
    service = CommandService(bot, commands)

    async def go():
        bot.bus.chat_message.emit(chat("!hola malo.com"))
        bot.bus.chat_message.emit(chat("!hola"))
        await svc.wait_idle()
        await asyncio.sleep(0.01)
    run(go())
    assert [t for _, t in bot.sent].count("hola!") == 1
    assert service is not None


# ------------------------------------------------------ comandos del chat
def test_comando_timeout(bot, svc, client, cmds):
    feed(bot, svc, chat("hola", user="Malo", user_id="u9"))
    reply = run(cmds.handle(chat("!timeout @malo 5m spam", user="mod", badges=MOD, user_id="m1")))
    assert reply == "Malo: timeout de 5 min."
    assert client.calls[-1] == ("timeout", "u9", 300)
    run(cmds.handle(chat("!timeout malo", user="mod", badges=MOD, user_id="m1")))
    assert client.calls[-1] == ("timeout", "u9", 600)
    a = svc.repo.recent_actions()[1]
    assert (a.action, a.reason, a.moderator, a.source) == ("timeout", "spam", "mod", "manual")


def test_comandos_ban_unban_y_busqueda(bot, svc, client, cmds):
    client.users["nuevo"] = ("u5", "Nuevo")
    assert run(cmds.handle(chat("!ban nuevo", user="mod", badges=MOD))) == "Nuevo: baneado."
    assert run(cmds.handle(chat("!unban @nuevo", user="mod", badges=MOD))) == "Nuevo: desbaneado."
    assert client.calls == [("ban", "u5"), ("unban", "u5")]
    assert "No encuentro" in run(cmds.handle(chat("!ban fantasma", user="mod", badges=MOD)))
    assert "no válido" in run(cmds.handle(chat("!ban <script>", user="mod", badges=MOD)))


def test_no_se_sanciona_a_mods_ni_al_bot(bot, svc, client, cmds):
    feed(bot, svc, chat("hola", user="OtroMod", user_id="u7", badges=MOD))
    assert "moderador" in run(cmds.handle(chat("!ban otromod", user="mod", badges=MOD)))
    client.users["alpekabot"] = (BOT_ID, "AlpekaBOT")
    assert "mí mismo" in run(cmds.handle(chat("!timeout alpekabot", user="mod", badges=MOD)))
    assert client.calls == []


def test_comandos_solo_para_mods(bot, svc, client, cmds):
    client.users["x"] = ("ux", "X")
    assert run(cmds.handle(chat("!ban x", badges=("vip",)))) is None
    assert client.calls == []


def test_comando_permit(bot, svc, client, cmds):
    reply = run(cmds.handle(chat("!permit @Pepe", user="mod", badges=MOD)))
    assert reply == "@Pepe puedes enviar un enlace durante 1 min."
    feed(bot, svc, chat("pepe.com"))
    assert client.calls == []
    assert svc.repo.recent_actions()[0].action == "permit"


def test_borrado_manual_desde_la_app(bot, svc, client):
    m = chat("mensaje feo")
    run(svc.delete(m))
    assert client.calls == [("delete", m.message_id)]
    a = svc.repo.recent_actions()[0]
    assert (a.action, a.moderator, a.message) == ("delete", "App", "mensaje feo")


# ------------------------------------------------------------ plataformas
def test_kick_timeout_en_minutos(monkeypatch):
    from alpekabot.config import KickConfig
    from alpekabot.core.event_bus import EventBus
    from alpekabot.platforms.kick.client import KickClient

    kc = KickClient(KickConfig(), EventBus())
    calls = []

    async def fake_moderate(what, fn, *args):
        calls.append(args)

    monkeypatch.setattr(kc, "_moderate", fake_moderate)
    monkeypatch.setattr(kc, "_channel_id", lambda: "42")
    for seconds in (30, 60, 61, 10**9):
        run(kc.timeout_user("7", seconds, "r"))
    assert [a[2] for a in calls] == [1, 1, 2, 10_080]
    run(kc.ban_user("7"))
    assert calls[-1] == ("42", "7", None, "")
