import asyncio

import pytest
from conftest import msg

from alpekabot.config import SongRequestConfig
from alpekabot.core.models import Permission, Platform
from alpekabot.features.songs import MAX_CONSECUTIVE_ERRORS, SongRepository, SongService
from alpekabot.features.youtube import VideoInfo, YouTubeError


def run(coro):
    return asyncio.run(coro)


def video(n: int, minutes: int = 3, **kw) -> VideoInfo:
    base = dict(video_id=f"vid{n:08d}", title=f"Canción {n}", channel="Canal", channel_id="UC1",
                duration_s=minutes * 60)
    base.update(kw)
    return VideoInfo(**base)


class FakeYouTube:
    def __init__(self):
        self.videos: dict[str, VideoInfo] = {}
        self.searches: list[str] = []
        self.error: str | None = None

    def add(self, v: VideoInfo) -> VideoInfo:
        self.videos[v.video_id] = v
        return v

    async def get_video(self, vid):
        if self.error:
            raise YouTubeError(self.error)
        return self.videos.get(vid)

    async def search(self, q):
        self.searches.append(q)
        return next(iter(self.videos.values()), None)


class FakePlayer:
    def __init__(self):
        self.calls: list = []
        self.on_ended = self.on_error = self.on_playing = None

    def load(self, vid, vol): self.calls.append(("load", vid, vol))
    def pause(self): self.calls.append(("pause",))
    def resume(self): self.calls.append(("resume",))
    def stop(self): self.calls.append(("stop",))
    def set_volume(self, v): self.calls.append(("volume", v))


@pytest.fixture
def yt():
    return FakeYouTube()


@pytest.fixture
def player():
    return FakePlayer()


@pytest.fixture
def svc(db, bot, yt, player, tmp_path):
    s = SongService(bot.bus, SongRepository(db, bot.bus), SongRequestConfig(), yt,
                    now_playing_path=tmp_path / "now.txt")
    s.attach_player(player)
    return s


def link(v: VideoInfo) -> str:
    return f"https://youtu.be/{v.video_id}"


# ------------------------------------------------------------ peticiones
def test_primera_peticion_suena_ya_y_la_segunda_se_encola(svc, yt, player, tmp_path):
    a, b = yt.add(video(1)), yt.add(video(2))
    assert run(svc.request(msg("!sr"), link(a))) == "@pepe 🎵 Suena ahora: Canción 1 (3:00)"
    assert player.calls[-1] == ("load", a.video_id, 50)
    assert (tmp_path / "now.txt").read_text(encoding="utf-8") == "Canción 1 — pedida por pepe"
    assert run(svc.request(msg("!sr", user="ana", user_id="u2"), link(b))) == \
        "@ana 🎵 Añadida: Canción 2 (3:00) — posición #1"


def test_busqueda_por_texto(svc, yt):
    yt.add(video(1))
    assert "Suena ahora" in run(svc.request(msg("!sr"), "never gonna give you up"))
    assert yt.searches == ["never gonna give you up"]


def test_al_terminar_pasa_a_la_siguiente(svc, yt, player):
    a, b = yt.add(video(1)), yt.add(video(2))
    run(svc.request(msg("!sr"), link(a)))
    run(svc.request(msg("!sr", user_id="u2"), link(b)))
    player.on_ended()
    assert player.calls[-1][1] == b.video_id
    assert [s.video_id for s in svc.repo.queue_list()] == [b.video_id]
    player.on_ended()
    assert svc.status.current is None and player.calls[-1] == ("stop",)


@pytest.mark.parametrize("kind,setup,expected", [
    ("cerrado", lambda s, y: setattr(s.config, "enabled", False), "cerradas"),
    ("permiso", lambda s, y: setattr(s.config, "permission", int(Permission.SUBSCRIBER)), "solo para"),
    ("vacio", None, "Uso:"),
])
def test_rechazos_basicos(svc, yt, kind, setup, expected):
    yt.add(video(1))
    if setup:
        setup(svc, yt)
    q = "" if kind == "vacio" else link(video(1))
    assert expected in run(svc.request(msg("!sr"), q))


def test_duracion_maxima_salvo_streamer(svc, yt):
    largo = yt.add(video(1, minutes=11))
    assert "el máximo es 10 min" in run(svc.request(msg("!sr"), link(largo)))
    assert "Suena ahora" in run(svc.request(msg("!sr", badges=("broadcaster",)), link(largo)))


def test_maximo_por_usuario(svc, yt):
    svc.config.max_per_user = 2
    for i in range(1, 5):
        yt.add(video(i))
    run(svc.request(msg("!sr"), link(video(1))))           # suena (no cuenta como pendiente)
    run(svc.request(msg("!sr"), link(video(2))))
    run(svc.request(msg("!sr"), link(video(3))))
    assert "Ya tienes 2 canciones" in run(svc.request(msg("!sr"), link(video(4))))
    assert "Añadida" in run(svc.request(msg("!sr", user_id="otro"), link(video(4))))


def test_duplicados_directos_y_no_incrustables(svc, yt):
    """Los no incrustables se aceptan: el reproductor los abre en la página de YouTube."""
    a = yt.add(video(1))
    run(svc.request(msg("!sr"), link(a)))
    assert "ya está en la cola" in run(svc.request(msg("!sr", user_id="u2"), link(a)))
    live = yt.add(video(2, is_live=True))
    assert "directos" in run(svc.request(msg("!sr"), link(live)))
    noemb = yt.add(video(3, embeddable=False))
    assert "Añadida" in run(svc.request(msg("!sr", user_id="u3"), link(noemb)))


def test_lista_negra(svc, yt):
    svc.repo.blacklist_add("word", "prohibida")
    svc.repo.blacklist_add("channel", "canal malo")
    svc.repo.blacklist_add("video", f"https://youtu.be/{video(3).video_id}")
    a = yt.add(video(1, title="Una canción PROHIBIDA"))
    b = yt.add(video(2, channel="Canal Malo"))
    c = yt.add(video(3))
    for v in (a, b, c):
        assert "no está permitida" in run(svc.request(msg("!sr"), link(v)))
    with pytest.raises(ValueError):
        svc.repo.blacklist_add("word", "PROHIBIDA")   # duplicado (sin distinguir mayúsculas)


def test_errores_de_youtube_llegan_al_chat(svc, yt):
    yt.error = "Se agotó la cuota diaria"
    assert run(svc.request(msg("!sr"), link(video(1)))) == "@pepe Se agotó la cuota diaria"
    yt.error = None
    assert "No encontré" in run(svc.request(msg("!sr"), link(video(9))))


# ------------------------------------------------------------ respaldo
def test_respaldo_solo_si_el_reproductor_esta_activo(svc, yt, player):
    svc.repo.backup_add(video(10))
    svc.repo.backup_add(video(11))
    assert svc.status.current is None              # al abrir no suena nada solo
    svc.play()
    assert svc.status.from_backup and svc.status.current.video_id == video(10).video_id
    player.on_ended()
    assert svc.status.current.video_id == video(11).video_id
    player.on_ended()
    assert svc.status.current.video_id == video(10).video_id   # en bucle


def test_peticion_espera_a_que_acabe_el_respaldo(svc, yt, player):
    svc.repo.backup_add(video(10))
    svc.play()
    a = yt.add(video(1))
    assert "posición #1" in run(svc.request(msg("!sr"), link(a)))
    assert svc.status.from_backup
    player.on_ended()
    assert not svc.status.from_backup and svc.status.current.video_id == a.video_id
    assert len(svc.repo.backup_list()) == 1        # el respaldo no se borra al sonar


# ------------------------------------------------------- controles y estado
def test_pausa_reanudar_volumen(svc, yt, player):
    run(svc.request(msg("!sr"), link(yt.add(video(1)))))
    svc.pause()
    assert svc.status.paused and player.calls[-1] == ("pause",)
    svc.play()
    assert not svc.status.paused and player.calls[-1] == ("resume",)
    assert svc.set_volume(150) == 100 and player.calls[-1] == ("volume", 100)


def test_stop_devuelve_la_peticion_a_la_cola(svc, yt):
    a = yt.add(video(1))
    run(svc.request(msg("!sr"), link(a)))
    svc.stop()
    q = svc.repo.queue_list()
    assert len(q) == 1 and not q[0].playing and svc.status.current is None


def test_cola_sobrevive_al_reinicio(db, bot, yt, svc):
    run(svc.request(msg("!sr"), link(yt.add(video(1)))))
    run(svc.request(msg("!sr", user_id="u2"), link(yt.add(video(2)))))
    nuevo = SongService(bot.bus, SongRepository(db, bot.bus), SongRequestConfig(), yt)
    q = nuevo.repo.queue_list()
    assert [s.video_id for s in q] == [video(1).video_id, video(2).video_id]
    assert not any(s.playing for s in q) and nuevo.status.current is None


def test_errores_seguidos_detienen_la_musica(svc, player):
    for i in range(MAX_CONSECUTIVE_ERRORS + 2):
        svc.repo.backup_add(video(100 + i))
    svc.play()
    for _ in range(MAX_CONSECUTIVE_ERRORS):
        player.on_error(150)
    assert svc.status.current is None and not svc.status.active


def test_mover_en_la_cola(svc, yt):
    for i in range(1, 5):
        run(svc.request(msg("!sr", user_id=f"u{i}"), link(yt.add(video(i)))))
    pending = svc.repo.queue_pending()
    svc.repo.queue_move(pending[2].id, -2)
    assert [s.title for s in svc.repo.queue_pending()] == ["Canción 4", "Canción 2", "Canción 3"]


# ------------------------------------------------------- comandos de chat
def test_comandos_de_chat(svc, yt, player):
    a, b = yt.add(video(1)), yt.add(video(2))
    run(svc._cmd_sr(msg("!sr"), [link(a)]))
    run(svc._cmd_sr(msg("!sr", user="ana", user_id="u2"), [link(b)]))
    assert run(svc._cmd_song(msg("!song"), [])) == "🎵 Suena: Canción 1 (3:00) — pedida por pepe"
    assert run(svc._cmd_queue(msg("!queue"), [])) == "Siguientes: 1. Canción 2 (ana)"
    assert run(svc._cmd_wrongsong(msg("!wrongsong", user="ana", user_id="u2"), [])) == \
        "@ana Quitada de la cola: Canción 2"
    assert run(svc._cmd_volume(msg("!volume"), ["30"])) == "🔊 Volumen: 30"
    assert "Uso" in run(svc._cmd_volume(msg("!volume"), ["mucho"]))
    assert run(svc._cmd_skip(msg("!skip"), [])) == "⏭ Saltada: Canción 1"
    assert "No está sonando" in run(svc._cmd_song(msg("!song"), []))


def test_comandos_registrados_con_permisos(db, bot, svc, yt):
    from alpekabot.features.commands import CommandRepository, CommandService, normalize_name

    cmds = CommandService(bot, CommandRepository(db, bot.bus))
    svc.register_commands(cmds)
    run(cmds.handle(msg(f"!sr {link(yt.add(video(1)))}")))
    assert svc.status.current is not None
    assert run(cmds.handle(msg("!skip"))) is None                      # viewer: ignorado
    assert run(cmds.handle(msg("!skip", badges=("moderator",)))).startswith("⏭")
    with pytest.raises(ValueError, match="interno"):
        normalize_name("sr")                                          # nombre reservado
    assert bot.sent and bot.sent[0][0] is Platform.TWITCH


# ------------------------------------------- errores del reproductor (código 150)
def test_evento_atrasado_de_la_cancion_anterior_se_ignora(svc, yt, player):
    a, b, c = yt.add(video(1)), yt.add(video(2)), yt.add(video(3))
    for i, v in enumerate((a, b, c)):
        run(svc.request(msg("!sr", user_id=f"u{i}"), link(v)))
    svc.skip()                                   # pasa a b
    player.on_error(150, a.video_id)             # llega tarde el error de a
    player.on_ended(a.video_id)                  # y su 'terminado'
    assert svc.status.current.video_id == b.video_id


def test_error_150_avisa_y_recuerda_el_video(db, bot, yt, player):
    avisos = []

    async def notify(platform, text):
        avisos.append((platform, text))

    s = SongService(bot.bus, SongRepository(db, bot.bus), SongRequestConfig(), yt, notify=notify)
    s.attach_player(player)
    a = yt.add(video(1, title="Canción con copyright"))

    async def main():
        await s.request(msg("!sr", user="ana", user_id="u2"), link(a))
        player.on_error(150, a.video_id)
        await asyncio.sleep(0)                   # deja salir el aviso
        return await s.request(msg("!sr", user="ana", user_id="u2"), link(a))

    again = run(main())
    assert s.status.current is None
    assert avisos == [(Platform.TWITCH, "@ana No se pudo reproducir «Canción con copyright»: "
                                        "el autor no permite reproducirla fuera de YouTube. Se salta.")]
    assert "no se pudo reproducir antes" in again   # rechazada al momento


def test_error_sin_cancion_actual_no_arranca_nada(svc, yt, player):
    svc.repo.backup_add(video(10))
    player.on_error(150, "loquesea")             # nada sonando: no debe empezar el respaldo
    assert svc.status.current is None and player.calls == []


def test_fallo_en_la_pagina_de_youtube_se_salta_y_se_recuerda(svc, yt, player):
    a, b = yt.add(video(1)), yt.add(video(2))
    run(svc.request(msg("!sr"), link(a)))
    run(svc.request(msg("!sr", user_id="u2"), link(b)))
    player.on_error(1000, a.video_id)            # tampoco se pudo en youtube.com/watch
    assert svc.status.current.video_id == b.video_id
    assert "no se pudo reproducir antes" in run(svc.request(msg("!sr", user_id="u3"), link(a)))


# ------------------------------------------------- interfaz (fase de rediseño)
def test_aviso_para_la_tarjeta_del_chat(svc, yt, bot):
    avisos = []
    bot.bus.song_queued.connect(avisos.append)
    a, b = yt.add(video(1)), yt.add(video(2))
    first = run(svc.request(msg("!sr"), link(a)))
    second = run(svc.request(msg("!sr", user="ana", user_id="u2"), link(b)))
    assert [(x.playing, x.position, x.reply) for x in avisos] == [(True, 0, first), (False, 1, second)]
    assert avisos[1].song.title == "Canción 2" and avisos[1].platform is Platform.TWITCH


def test_peticion_desde_la_app_sin_limites(svc, yt, player):
    largo = yt.add(video(1, minutes=60))  # supera el máximo de 10 min, pero es el streamer
    song = run(svc.add_from_app(link(largo), "LoonVT", Platform.KICK))
    assert song.requested_by == "LoonVT" and svc.status.current.video_id == largo.video_id
    with pytest.raises(YouTubeError, match="ya está en la cola"):
        yt.add(video(2))
        run(svc.add_from_app(link(video(2)), "LoonVT", Platform.KICK))
        run(svc.add_from_app(link(video(2)), "LoonVT", Platform.KICK))
