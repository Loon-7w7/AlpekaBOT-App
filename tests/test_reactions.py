"""Reacciones del avatar: coincidencia, validación, cola, cooldown y devolución de canjes."""

import asyncio
from dataclasses import replace

import pytest

from alpekabot.core.event_bus import EventBus
from alpekabot.core.models import ChannelEvent, EventKind, Platform
from alpekabot.features.reactions import (
    EXPRESSION,
    HOTKEY,
    MAX_QUEUE,
    MAX_STEPS,
    WAIT,
    Reaction,
    ReactionRepository,
    ReactionService,
    RewardLink,
    Step,
)
from alpekabot.storage import database
from alpekabot.integrations.vtube_studio import VTSError
from alpekabot.platforms.base import PlatformError

from conftest import FakeClock

TW = frozenset({Platform.TWITCH})
HK = Step(HOTKEY, "hk1", "Saludo")


class FakeAvatar:
    def __init__(self, connected=True, fail_on=None):
        self.is_connected = connected
        self.calls = []
        self.fail_on = fail_on

    async def trigger_hotkey(self, hotkey_id):
        if hotkey_id == self.fail_on:
            raise VTSError("el modelo no tiene esa hotkey")
        self.calls.append(("hotkey", hotkey_id))

    async def set_expression(self, file, active, fade_s=0.25):
        self.calls.append(("expression", file, active))


class Harness:
    def __init__(self, db, avatar=None):
        self.bus = EventBus()
        self.repo = ReactionRepository(db, self.bus)
        self.avatar = avatar or FakeAvatar()
        self.sent, self.completed, self.slept = [], [], []
        self.clock = FakeClock()
        self.svc = ReactionService(self.bus, self.repo, self.avatar, self._send, self._complete,
                                   context=lambda p: ("canal", None), clock=self.clock, sleep=self._sleep)

    async def _send(self, platform, text):
        self.sent.append((platform, text))

    async def _complete(self, platform, reward_id, redemption_id, refund):
        self.completed.append((reward_id, redemption_id, refund))

    async def _sleep(self, seconds):
        self.slept.append(seconds)
        self.avatar.calls.append(("sleep", seconds))
        await asyncio.sleep(0)

    async def settle(self):
        """Deja que la cola termine (el sueño falso cede el control una vez por paso)."""
        for _ in range(200):
            await asyncio.sleep(0)


@pytest.fixture
def h(db):
    return Harness(db)


def follow(platform=Platform.TWITCH, user="pepe"):
    return ChannelEvent(platform, EventKind.FOLLOW, "u1", user)


def redeem(reward="r1", pending=True, redemption="c1", platform=Platform.TWITCH):
    return ChannelEvent(platform, EventKind.REWARD, "u1", "pepe", 100, reward, "Saludo", redemption, "hola",
                        pending=pending)


def reward_reaction(name="Canje", **over):
    data = dict(name=name, trigger=EventKind.REWARD, steps=(HK,), platforms=TW,
                rewards=(RewardLink(Platform.TWITCH, "r1", "Saludo"),))
    return Reaction(**{**data, **over})


# ------------------------------------------------------------- coincidencia
def test_coincide_por_tipo_plataforma_y_minimo():
    r = Reaction("Bits", EventKind.BITS, (HK,), platforms=TW, min_amount=100)
    assert r.matches(ChannelEvent(Platform.TWITCH, EventKind.BITS, amount=100))
    assert not r.matches(ChannelEvent(Platform.TWITCH, EventKind.BITS, amount=99))
    assert not r.matches(ChannelEvent(Platform.KICK, EventKind.BITS, amount=500))
    assert not r.matches(ChannelEvent(Platform.TWITCH, EventKind.GIFT, amount=500))


def test_un_regalo_de_n_subs_respeta_el_minimo():
    r = Reaction("Regalo", EventKind.GIFT, (HK,), min_amount=5)
    assert r.matches(ChannelEvent(Platform.KICK, EventKind.GIFT, amount=5))
    assert not r.matches(ChannelEvent(Platform.KICK, EventKind.GIFT, amount=4))


def test_canje_solo_de_su_premio_y_desactivada_nunca():
    r = reward_reaction()
    assert r.matches(redeem("r1"))
    assert not r.matches(redeem("otro"))
    assert not reward_reaction(rewards=()).matches(redeem(""))
    assert not reward_reaction(enabled=False).matches(redeem("r1"))


def test_canje_multiplataforma_salta_con_el_premio_de_cada_una():
    r = reward_reaction(platforms=frozenset(Platform), rewards=(
        RewardLink(Platform.TWITCH, "tw1", "Saludo"), RewardLink(Platform.KICK, "kk1", "Hola")))
    assert r.matches(ChannelEvent(Platform.TWITCH, EventKind.REWARD, reward_id="tw1"))
    assert r.matches(ChannelEvent(Platform.KICK, EventKind.REWARD, reward_id="kk1"))
    # El ID de un premio solo vale en su plataforma
    assert not r.matches(ChannelEvent(Platform.KICK, EventKind.REWARD, reward_id="tw1"))
    assert not r.matches(ChannelEvent(Platform.TWITCH, EventKind.REWARD, reward_id="kk1"))


def test_guardar_canje_multiplataforma_deduce_las_plataformas(h):
    h.repo.add(reward_reaction(platforms=TW, rewards=(
        RewardLink(Platform.KICK, "kk1", "Hola"), RewardLink(Platform.TWITCH, "tw1", "Saludo á"))))
    got = h.repo.list_all()[0]
    assert got.platforms == frozenset(Platform)
    assert got.rewards == (RewardLink(Platform.TWITCH, "tw1", "Saludo á"), RewardLink(Platform.KICK, "kk1", "Hola"))


def test_reaccion_que_no_es_canje_no_guarda_premios(h):
    h.repo.add(Reaction("a", EventKind.FOLLOW, (HK,), rewards=(RewardLink(Platform.KICK, "kk1"),)))
    assert h.repo.list_all()[0].rewards == ()


def test_migracion_v5_conserva_las_reacciones(tmp_path):
    import json
    import sqlite3

    path = tmp_path / "v4.db"
    conn = sqlite3.connect(path)
    for script in database._MIGRATIONS[:4]:
        conn.executescript(script)
    conn.execute("PRAGMA user_version = 4")
    insert = ("INSERT INTO reactions (name, trigger, reward_id, reward_title, platforms, steps, created_at, updated_at)"
              " VALUES (?,?,?,?,?,?,'t','t')")
    steps = json.dumps([HK.to_dict()])
    conn.execute(insert, ("Canje K", "reward", "k1", "Hola «ñ»", "kick", steps))
    conn.execute(insert, ("Canje T", "reward", "t1", "Saludo", "twitch", steps))
    conn.execute(insert, ("Follow", "follow", "", "", "kick,twitch", steps))
    conn.commit()
    conn.close()

    db = database.Database(path)
    try:
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == len(database._MIGRATIONS)
        got = {r.name: r for r in ReactionRepository(db, EventBus()).list_all()}
        assert got["Canje K"].rewards == (RewardLink(Platform.KICK, "k1", "Hola «ñ»"),)
        assert got["Canje K"].platforms == frozenset({Platform.KICK})
        assert got["Canje T"].rewards == (RewardLink(Platform.TWITCH, "t1", "Saludo"),)
        assert got["Follow"].rewards == () and got["Follow"].steps == (HK,)
    finally:
        db.close()


# --------------------------------------------------------------- repositorio
def test_guardar_y_leer_conserva_todo(h):
    r = h.repo.add(Reaction("  Fiesta ", EventKind.RAID, (HK, Step(WAIT, seconds=1.5),
                                                          Step(EXPRESSION, "e.exp3.json", "Risa", 2, True)),
                            message="Raid de $user", cooldown_s=30, platforms=TW, min_amount=10))
    got = h.repo.list_all()[0]
    assert got.id == r.id and got.name == "Fiesta"
    assert got.steps == r.steps and got.message == "Raid de $user"
    assert (got.cooldown_s, got.min_amount, got.platforms) == (30, 10, TW)
    h.repo.update(replace(got, enabled=False))
    assert not h.repo.list_all()[0].enabled
    h.repo.delete(got.id)
    assert h.repo.list_all() == []


@pytest.mark.parametrize("bad, error", [
    (Reaction("", EventKind.FOLLOW, (HK,)), "nombre"),
    (Reaction("x" * 41, EventKind.FOLLOW, (HK,)), "nombre"),
    (Reaction("a", EventKind.FOLLOW, (HK,), platforms=frozenset()), "plataforma"),
    (Reaction("a", EventKind.REWARD, (HK,), platforms=TW), "premio"),
    (Reaction("a", EventKind.REWARD, (HK,), platforms=frozenset(), rewards=()), "premio"),
    (Reaction("a", EventKind.REWARD, (HK,), rewards=(RewardLink(Platform.KICK, "r1"), RewardLink(Platform.KICK, "r2"))),
     "por plataforma"),
    (Reaction("a", EventKind.RAID, (HK,), platforms=frozenset({Platform.KICK})), "Twitch"),
    (Reaction("a", EventKind.FOLLOW), "al menos un paso"),
    (Reaction("a", EventKind.FOLLOW, (HK,) * (MAX_STEPS + 1)), "Máximo"),
    (Reaction("a", EventKind.FOLLOW, (Step("rara"),)), "desconocido"),
    (Reaction("a", EventKind.FOLLOW, (Step(HOTKEY),)), "sin hotkey"),
    (Reaction("a", EventKind.FOLLOW, (Step(WAIT),)), "duración"),
    (Reaction("a", EventKind.FOLLOW, (Step(WAIT, seconds=121),)), "duración"),
    (Reaction("a", EventKind.FOLLOW, message="x" * 501), "supera"),
    (Reaction("a", EventKind.FOLLOW, (HK,), cooldown_s=-1), "cooldown"),
])
def test_validacion(h, bad, error):
    with pytest.raises(ValueError, match=error):
        h.repo.add(bad)


def test_nombre_duplicado(h):
    h.repo.add(Reaction("a", EventKind.FOLLOW, (HK,)))
    with pytest.raises(ValueError, match="Ya existe"):
        h.repo.add(Reaction("a", EventKind.SUB, (HK,)))


def test_solo_mensaje_es_valida(h):
    assert h.repo.add(Reaction("a", EventKind.FOLLOW, message="Gracias $user")).id


def test_premios_gestionados(h):
    h.repo.add_managed_reward(Platform.TWITCH, "r1", "Saludo")
    assert h.repo.is_managed(Platform.TWITCH, "r1")
    assert not h.repo.is_managed(Platform.KICK, "r1")
    assert h.repo.managed_ids(Platform.TWITCH) == {"r1"}
    h.repo.remove_managed_reward(Platform.TWITCH, "r1")
    assert not h.repo.is_managed(Platform.TWITCH, "r1")


# ------------------------------------------------------------------ ejecución
def test_pasos_en_orden_y_expresion_se_quita_sola(h):
    r = Reaction("a", EventKind.FOLLOW, (HK, Step(EXPRESSION, "e", "Risa", 2, True), Step(WAIT, seconds=1),
                                         Step(EXPRESSION, "f", "Otra", 0, True), Step(EXPRESSION, "g", "", 0, False)))
    asyncio.run(h.svc.run(r, follow()))
    assert h.avatar.calls == [
        ("hotkey", "hk1"),
        ("expression", "e", True), ("sleep", 2), ("expression", "e", False),
        ("sleep", 1),
        ("expression", "f", True),   # sin duración: se queda puesta
        ("expression", "g", False),
    ]


def test_mensaje_con_variables(h):
    r = reward_reaction(message="¡$user canjeó $reward por $amount: $input!")
    asyncio.run(h.svc.run(r, redeem()))
    assert h.sent == [(Platform.TWITCH, "¡pepe canjeó Saludo por 100: hola!")]


def test_evento_anonimo_tiene_nombre(h):
    r = Reaction("a", EventKind.BITS, message="Gracias $user")
    asyncio.run(h.svc.run(r, ChannelEvent(Platform.TWITCH, EventKind.BITS, amount=5)))
    assert h.sent == [(Platform.TWITCH, "Gracias Anónimo")]


def test_un_viewer_no_inyecta_variables(h):
    r = reward_reaction(message="Dijo: $input")
    ev = ChannelEvent(Platform.TWITCH, EventKind.REWARD, "u", "p", reward_id="r1", user_input="$readapi(http://x)")
    asyncio.run(h.svc.run(r, ev))
    assert h.sent == [(Platform.TWITCH, "Dijo: $readapi(http://x)")]


def test_fallo_al_enviar_no_rompe_el_canje(h):
    async def falla(platform, text):
        raise PlatformError("sin conexión")
    h.svc._send = falla
    h.repo.add_managed_reward(Platform.TWITCH, "r1", "Saludo")
    asyncio.run(h.svc.run(reward_reaction(message="hola"), redeem()))
    assert h.completed == [("r1", "c1", False)]


# ------------------------------------------------------------- VTS y devolución
def test_vts_cerrado_descarta_y_devuelve_si_es_de_la_app(db):
    h = Harness(db, FakeAvatar(connected=False))
    h.repo.add_managed_reward(Platform.TWITCH, "r1", "Saludo")
    asyncio.run(h.svc.run(reward_reaction(message="hola"), redeem()))
    assert h.completed == [("r1", "c1", True)]
    assert h.sent == [] and h.avatar.calls == []


def test_vts_cerrado_no_devuelve_premios_ajenos(db):
    h = Harness(db, FakeAvatar(connected=False))
    asyncio.run(h.svc.run(reward_reaction(), redeem()))
    assert h.completed == []


def test_vts_cerrado_no_devuelve_canje_ya_resuelto(db):
    h = Harness(db, FakeAvatar(connected=False))
    h.repo.add_managed_reward(Platform.TWITCH, "r1", "Saludo")
    asyncio.run(h.svc.run(reward_reaction(), redeem(pending=False)))
    assert h.completed == []


def test_reaccion_solo_mensaje_funciona_con_vts_cerrado(db):
    h = Harness(db, FakeAvatar(connected=False))
    asyncio.run(h.svc.run(Reaction("a", EventKind.FOLLOW, message="Gracias $user"), follow()))
    assert h.sent == [(Platform.TWITCH, "Gracias pepe")]


def test_todo_bien_completa_el_canje_de_la_app(h):
    h.repo.add_managed_reward(Platform.TWITCH, "r1", "Saludo")
    asyncio.run(h.svc.run(reward_reaction(), redeem()))
    assert h.completed == [("r1", "c1", False)]


def test_premio_ajeno_no_se_completa(h):
    asyncio.run(h.svc.run(reward_reaction(), redeem()))
    assert h.completed == []


def test_primer_paso_falla_devuelve(db):
    h = Harness(db, FakeAvatar(fail_on="hk1"))
    h.repo.add_managed_reward(Platform.TWITCH, "r1", "Saludo")
    asyncio.run(h.svc.run(reward_reaction(message="hola"), redeem()))
    assert h.completed == [("r1", "c1", True)] and h.sent == []


def test_paso_posterior_falla_no_devuelve(db):
    h = Harness(db, FakeAvatar(fail_on="malo"))
    h.repo.add_managed_reward(Platform.TWITCH, "r1", "Saludo")
    r = reward_reaction(steps=(HK, Step(HOTKEY, "malo")), message="hola")
    asyncio.run(h.svc.run(r, redeem()))
    assert h.completed == [("r1", "c1", False)] and len(h.sent) == 1


def test_error_al_devolver_se_captura(db):
    h = Harness(db, FakeAvatar(connected=False))

    async def falla(*_):
        raise PlatformError("Twitch dijo que no")
    h.svc._complete = falla
    h.repo.add_managed_reward(Platform.TWITCH, "r1", "Saludo")
    asyncio.run(h.svc.run(reward_reaction(), redeem()))  # no lanza


# ---------------------------------------------------------- cola y cooldown
def test_cola_una_detras_de_otra(h):
    h.repo.add(Reaction("a", EventKind.FOLLOW, (Step(HOTKEY, "a1"), Step(WAIT, seconds=1), Step(HOTKEY, "a2"))))
    h.repo.add(Reaction("b", EventKind.FOLLOW, (Step(HOTKEY, "b1"), Step(HOTKEY, "b2"))))

    async def main():
        h.svc.start()
        h.bus.channel_event.emit(follow())
        await h.settle()
        await h.svc.stop()

    asyncio.run(main())
    assert h.avatar.calls == [("hotkey", "a1"), ("sleep", 1), ("hotkey", "a2"), ("hotkey", "b1"), ("hotkey", "b2")]


def test_cooldown_ignora_y_luego_vuelve(h):
    h.repo.add(Reaction("a", EventKind.FOLLOW, (HK,), cooldown_s=30))

    async def main():
        h.svc.start()
        h.svc.handle(follow())
        h.svc.handle(follow())
        h.clock.advance(29)
        h.svc.handle(follow())
        await h.settle()
        assert len(h.avatar.calls) == 1
        h.clock.advance(1)
        h.svc.handle(follow())
        await h.settle()
        await h.svc.stop()

    asyncio.run(main())
    assert len(h.avatar.calls) == 2


def test_cooldown_es_por_reaccion(h):
    h.repo.add(Reaction("a", EventKind.FOLLOW, (Step(HOTKEY, "a"),), cooldown_s=30))
    h.repo.add(Reaction("b", EventKind.FOLLOW, (Step(HOTKEY, "b"),)))

    async def main():
        h.svc.start()
        h.svc.handle(follow())
        h.svc.handle(follow())
        await h.settle()
        await h.svc.stop()

    asyncio.run(main())
    assert h.avatar.calls == [("hotkey", "a"), ("hotkey", "b"), ("hotkey", "b")]


def test_varias_reacciones_al_mismo_canje_lo_completan_una_vez(h):
    h.repo.add_managed_reward(Platform.TWITCH, "r1", "Saludo")
    h.repo.add(reward_reaction("uno"))
    h.repo.add(reward_reaction("dos"))

    async def main():
        h.svc.start()
        h.svc.handle(redeem())
        await h.settle()
        await h.svc.stop()

    asyncio.run(main())
    assert len(h.avatar.calls) == 2
    assert h.completed == [("r1", "c1", False)]


def test_cola_llena_descarta(h):
    h.repo.add(Reaction("a", EventKind.FOLLOW, (HK,)))

    async def main():
        h.svc._queue = asyncio.Queue()  # sin trabajador: nada sale de la cola
        for _ in range(MAX_QUEUE + 5):
            h.svc.handle(follow())
        assert h.svc.pending == MAX_QUEUE

    asyncio.run(main())


def test_sin_arrancar_no_explota(h):
    h.repo.add(Reaction("a", EventKind.FOLLOW, (HK,)))
    h.svc.handle(follow())
    assert h.avatar.calls == []


def test_un_error_no_detiene_la_cola(h):
    h.repo.add(Reaction("a", EventKind.FOLLOW, (HK,)))
    real = h.avatar.trigger_hotkey
    failed = []

    async def explota(hotkey_id):
        if not failed:
            failed.append(1)
            raise RuntimeError("inesperado")
        await real(hotkey_id)

    h.avatar.trigger_hotkey = explota

    async def main():
        h.svc.start()
        h.svc.handle(follow())
        h.svc.handle(follow())
        await h.settle()
        await h.svc.stop()

    asyncio.run(main())
    assert h.avatar.calls == [("hotkey", "hk1")]


# --------------------------------------------------------------------- prueba
def test_probar_no_escribe_ni_consume_cooldown_ni_toca_el_canje(h):
    h.repo.add_managed_reward(Platform.TWITCH, "r1", "Saludo")
    r = h.repo.add(reward_reaction(message="hola", cooldown_s=60))

    async def main():
        h.svc.start()
        h.svc.test(r)
        await h.settle()
        h.svc.handle(redeem())  # el cooldown sigue libre
        await h.settle()
        await h.svc.stop()

    asyncio.run(main())
    assert h.avatar.calls == [("hotkey", "hk1"), ("hotkey", "hk1")]
    assert len(h.sent) == 1 and h.completed == [("r1", "c1", False)]
