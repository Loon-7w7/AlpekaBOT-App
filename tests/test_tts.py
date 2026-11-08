"""TTS: comando configurable, permisos, cooldown, limpieza del texto, cola y sanciones."""

from __future__ import annotations

import asyncio

import pytest

from alpekabot.config import AppConfig, TtsConfig
from alpekabot.core.models import Permission, Platform
from alpekabot.features import commands as commands_module
from alpekabot.features.commands import Command, CommandRepository, CommandService, normalize_name
from alpekabot.features.moderation import ModAction
from alpekabot.features.tts import SKIP_COMMAND, TtsService, Voice, clean_text, voice_label
from conftest import FakeClient, FakeClock, msg


class FakePlayer:
    def __init__(self, hold: bool = False):
        self.played: list[tuple[bytes, int, str]] = []
        self.volume = None
        self.hold = hold
        self.release: asyncio.Event | None = None

    async def play(self, audio, volume, device):
        self.played.append((audio, volume, device))
        if self.hold:
            self.release = asyncio.Event()
            await self.release.wait()

    def set_volume(self, volume):
        self.volume = volume


class Harness:
    def __init__(self, bot, db, **cfg):
        self.spoken: list[tuple[str, str, int]] = []
        self.saves = 0
        self.clock = FakeClock()
        self.bot = bot
        cfg.setdefault("enabled", True)
        self.config = TtsConfig(**cfg)
        self.repo = CommandRepository(db, bot.bus)
        self.commands = CommandService(bot, self.repo)
        self.svc = TtsService(bot.bus, self.config, synthesize=self._synth, save_config=self._save, clock=self.clock)
        self.svc.register_commands(self.commands)
        self.fail = None

    async def _synth(self, text, voice, rate):
        if self.fail:
            raise self.fail
        self.spoken.append((text, voice, rate))
        return b"mp3"

    def _save(self):
        self.saves += 1

    def run(self, m):
        return asyncio.run(self.commands.handle(m))


@pytest.fixture(autouse=True)
def free_reserved_names():
    """RESERVED_NAMES es global: otros tests pueden usar estos nombres."""
    before = set(commands_module.RESERVED_NAMES)
    yield
    commands_module.RESERVED_NAMES.clear()
    commands_module.RESERVED_NAMES.update(before)


@pytest.fixture
def h(bot, db):
    return Harness(bot, db)


async def settle(rounds: int = 20):
    for _ in range(rounds):
        await asyncio.sleep(0)


# ------------------------------------------------------------ limpieza del texto
@pytest.mark.parametrize("raw, expected", [
    ("hola mundo", "hola mundo"),
    ("  hola \n\t mundo ", "hola mundo"),
    ("mira https://evil.example/x esto", "mira esto"),
    ("entra a pagina.com/oferta ya", "entra a ya"),
    ("holaaaaaaaaa", "holaaa"),
    ("no​che", "no che"),
    ("!!!???", ""),
    ("https://solo.un.enlace.com", ""),
    ("", ""),
])
def test_clean_text(raw, expected):
    assert clean_text(raw) == expected


def test_clean_text_cuts_at_last_space():
    assert clean_text("uno dos tres cuatro", 10) == "uno dos"
    assert clean_text("abcdefghijkl", 5) == "abcde"


def test_voice_label():
    assert voice_label("es-MX-DaliaNeural", "Female") == "es-MX · Dalia (mujer)"
    assert voice_label("en-US-EmmaMultilingualNeural", "Male") == "en-US · EmmaMultilingual (hombre)"


# ---------------------------------------------------------------- el comando
def test_command_queues_message(h):
    assert h.run(msg("!tts hola  mundo", "Pepe")) is None
    assert [i.text for i in h.svc.pending] == ["hola mundo"]
    assert h.svc.pending[0].platform is Platform.TWITCH
    assert h.bot.sent == []


def test_works_on_kick_too(h):
    h.bot.platforms[Platform.KICK] = FakeClient(Platform.KICK)
    h.run(msg("!tts desde kick", platform=Platform.KICK))
    assert h.svc.pending[0].platform is Platform.KICK


def test_disabled_ignores_command(bot, db):
    h = Harness(bot, db, enabled=False)
    h.run(msg("!tts hola"))
    assert h.svc.pending == []
    assert bot.sent == []


def test_empty_message_shows_usage(h):
    h.run(msg("!tts https://a.com"))
    assert h.svc.pending == []
    assert h.bot.sent[-1][1] == "@pepe Uso: !tts mensaje"


def test_max_chars_applies(bot, db):
    h = Harness(bot, db, max_chars=10)
    h.run(msg("!tts uno dos tres cuatro"))
    assert h.svc.pending[0].text == "uno dos"


def test_read_name_option(bot, db):
    h = Harness(bot, db, read_name=True)
    h.run(msg("!tts hola", "pepe_loco"))
    item = h.svc.pending[0]
    assert item.spoken == "pepe loco dice: hola"
    assert item.text == "hola"


def test_without_read_name_only_the_message(h):
    h.run(msg("!tts hola", "Pepe"))
    assert h.svc.pending[0].spoken == "hola"


def test_cooldown_is_global_and_expires(h):
    h.run(msg("!tts uno"))
    h.run(msg("!tts dos", "otro", user_id="u2"))
    assert len(h.svc.pending) == 1
    h.clock.advance(10)
    h.run(msg("!tts tres", "otro", user_id="u2"))
    assert [i.text for i in h.svc.pending] == ["uno", "tres"]


def test_invalid_message_does_not_spend_cooldown(h):
    h.run(msg("!tts ???"))
    h.run(msg("!tts ahora sí"))
    assert [i.text for i in h.svc.pending] == ["ahora sí"]


def test_queue_limit_replies(bot, db):
    h = Harness(bot, db, max_queue=1, cooldown_s=0)
    h.run(msg("!tts uno"))
    h.run(msg("!tts dos"))
    assert [i.text for i in h.svc.pending] == ["uno"]
    assert "llena" in bot.sent[-1][1]


def test_permission_is_configurable(h):
    h.svc.update(permission=int(Permission.VIP))
    h.run(msg("!tts viewer"))
    assert h.svc.pending == []
    h.run(msg("!tts vip", badges=("vip",)))
    assert [i.text for i in h.svc.pending] == ["vip"]
    assert ("tts", Permission.VIP) in h.commands.builtins()


def test_bot_own_message_is_ignored(h):
    h.run(msg("!tts bucle", user_id="bot-id"))
    assert h.svc.pending == []


# ------------------------------------------------------- cambiar el comando
def test_rename_command(h):
    assert h.svc.set_command(" !LEER ") == "leer"
    h.run(msg("!tts antiguo"))
    assert h.svc.pending == []
    h.run(msg("!leer nuevo"))
    assert [i.text for i in h.svc.pending] == ["nuevo"]
    assert h.config.command == "leer"
    assert h.saves == 1


def test_rename_frees_old_name_and_reserves_new(h):
    h.svc.set_command("leer")
    assert normalize_name("tts") == "tts"
    with pytest.raises(ValueError):
        normalize_name("leer")


def test_rename_keeps_permission(h):
    h.svc.update(permission=int(Permission.MODERATOR))
    h.svc.set_command("leer")
    assert ("leer", Permission.MODERATOR) in h.commands.builtins()


@pytest.mark.parametrize("bad", ["", "dos palabras", "a-b", "x" * 33])
def test_rename_rejects_invalid(h, bad):
    with pytest.raises(ValueError):
        h.svc.set_command(bad)
    assert h.config.command == "tts"


def test_rename_rejects_builtin_and_skip(h):
    for taken in ("addcmd", SKIP_COMMAND):
        with pytest.raises(ValueError):
            h.svc.set_command(taken)
    assert h.config.command == "tts"


def test_rename_rejects_custom_command(h):
    h.repo.add(Command(name="hola", response="x"))
    with pytest.raises(ValueError, match="!hola"):
        h.svc.set_command("hola")
    h.run(msg("!tts sigue"))
    assert len(h.svc.pending) == 1


def test_rename_to_same_name_is_noop(h):
    assert h.svc.set_command("!TTS") == "tts"
    assert h.saves == 0


def test_saved_command_is_used_at_startup(bot, db):
    h = Harness(bot, db, command="leer")
    h.run(msg("!leer hola"))
    assert len(h.svc.pending) == 1


def test_handedited_config_cannot_replace_builtin(bot, db):
    commands = CommandService(bot, CommandRepository(db, bot.bus))
    cfg = TtsConfig(command="addcmd")
    TtsService(bot.bus, cfg).register_commands(commands)
    assert cfg.command == "tts"
    assert ("addcmd", Permission.MODERATOR) in commands.builtins()


def test_config_loads_section_and_old_files(tmp_path, monkeypatch):
    import json

    from alpekabot import config as config_module
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"tts": {"command": "voz", "desconocido": 1}}), encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_FILE", path)
    assert AppConfig.load().tts.command == "voz"
    path.write_text("{}", encoding="utf-8")
    assert AppConfig.load().tts == TtsConfig()


def test_disabled_by_default():
    assert TtsConfig().enabled is False


# ------------------------------------------------------------------ ajustes
def test_update_clamps_values(h):
    player = FakePlayer()
    h.svc.attach_player(player)
    h.svc.update(rate=500, volume=-3, max_chars=1, cooldown_s=-1, max_queue=999)
    c = h.config
    assert (c.rate, c.volume, c.max_chars, c.cooldown_s, c.max_queue) == (50, 0, 10, 0, 50)
    assert player.volume == 0


def test_update_rejects_unknown_and_command(h):
    for bad in ({"nada": 1}, {"command": "skip"}):
        with pytest.raises(ValueError):
            h.svc.update(**bad)
    assert h.config.command == "tts"


# ------------------------------------------------------------------- la cola
def test_plays_in_order_with_configured_voice_and_device(bot, db):
    h = Harness(bot, db, voice="es-ES-ElviraNeural", rate=10, volume=70, output_device="dev1", cooldown_s=0)
    player = FakePlayer()
    h.svc.attach_player(player)

    async def main():
        h.svc.start()
        await h.commands.handle(msg("!tts uno"))
        await h.commands.handle(msg("!tts dos"))
        await settle()
        await h.svc.stop()

    asyncio.run(main())
    assert h.spoken == [("uno", "es-ES-ElviraNeural", 10), ("dos", "es-ES-ElviraNeural", 10)]
    assert player.played == [(b"mp3", 70, "dev1")] * 2
    assert h.svc.current is None


def test_skip_stops_current_and_continues(h):
    h.svc.attach_player(FakePlayer(hold=True))

    async def main():
        h.svc.start()
        h.svc.preview("uno")
        h.svc.preview("dos")
        await settle()
        assert h.svc.current.text == "uno"
        h.svc.skip()
        await settle()
        assert h.svc.current.text == "dos"
        assert h.svc.pending == []
        await h.svc.stop()

    asyncio.run(main())
    assert [s[0] for s in h.spoken] == ["uno", "dos"]


def test_skiptts_is_for_mods_only(h):
    h.svc.attach_player(FakePlayer(hold=True))

    async def main():
        h.svc.start()
        h.svc.preview("uno")
        await settle()
        await h.commands.handle(msg("!skiptts"))
        await settle()
        assert h.svc.current is not None
        await h.commands.handle(msg("!skiptts", badges=("moderator",)))
        await settle()
        assert h.svc.current is None
        await h.svc.stop()

    asyncio.run(main())


def test_synth_failure_is_reported_and_queue_continues(h):
    player = FakePlayer()
    h.svc.attach_player(player)
    h.fail = OSError("sin red con datos secretos")

    async def main():
        h.svc.start()
        h.svc.preview("uno")
        await settle()
        assert "OSError" in h.svc.last_error
        assert "secretos" not in h.svc.last_error
        h.fail = None
        h.svc.preview("dos")
        await settle()
        assert h.svc.last_error == ""
        await h.svc.stop()

    asyncio.run(main())
    assert len(player.played) == 1


def test_stop_cancels_message_in_progress(h):
    h.svc.attach_player(FakePlayer(hold=True))

    async def main():
        h.svc.start()
        h.svc.preview("uno")
        h.svc.preview("dos")
        await settle()
        await asyncio.wait_for(h.svc.stop(), 2)
        assert h.svc.pending == []
        assert h.svc._speaking.cancelled()  # noqa: SLF001

    asyncio.run(main())


def test_remove_and_clear(h):
    h.svc.preview("uno")
    h.svc.preview("dos")
    h.svc.preview("tres")
    h.svc.remove(h.svc.pending[1].id)
    assert [i.text for i in h.svc.pending] == ["uno", "tres"]
    h.svc.clear()
    assert h.svc.pending == []


def test_preview_works_when_disabled(bot, db):
    h = Harness(bot, db, enabled=False)
    assert h.svc.preview("hola") is True
    assert h.svc.preview("???") is False
    assert len(h.svc.pending) == 1


# ---------------------------------------------------------------- moderación
def _action(action, user_id="u1", platform=Platform.TWITCH):
    return ModAction(platform, user_id, "pepe", action)


@pytest.mark.parametrize("action", ["warn", "delete", "timeout", "ban"])
def test_punished_user_leaves_queue(h, action):
    h.svc.update(cooldown_s=0)
    h.run(msg("!tts malo"))
    h.run(msg("!tts bueno", "otro", user_id="u2"))
    h.bot.bus.mod_action.emit(_action(action))
    assert [i.text for i in h.svc.pending] == ["bueno"]


@pytest.mark.parametrize("action", ["unban", "permit"])
def test_non_punishment_keeps_queue(h, action):
    h.run(msg("!tts hola"))
    h.bot.bus.mod_action.emit(_action(action))
    assert len(h.svc.pending) == 1


def test_same_id_on_other_platform_is_not_affected(h):
    h.run(msg("!tts hola"))
    h.bot.bus.mod_action.emit(_action("ban", platform=Platform.KICK))
    assert len(h.svc.pending) == 1


def test_punishment_silences_current_message(h):
    h.svc.attach_player(FakePlayer(hold=True))

    async def main():
        h.svc.start()
        await h.commands.handle(msg("!tts malo"))
        await settle()
        assert h.svc.current is not None
        h.bot.bus.mod_action.emit(_action("ban"))
        await settle()
        assert h.svc.current is None
        await h.svc.stop()

    asyncio.run(main())


def test_blocked_message_is_not_queued(h):
    class Mod:
        @staticmethod
        def is_blocked(_m):
            return True

    h.bot.moderation = Mod()

    async def main():
        h.commands._on_message(msg("!tts prohibido"))  # noqa: SLF001
        await settle()

    asyncio.run(main())
    assert h.svc.pending == []


# ---------------------------------------------------------------- la página
def test_page_renames_and_shows_error(h):
    from alpekabot.ui.tts_tab import TtsTab

    page = TtsTab(h.svc, h.bot.bus)
    page._command.setText("addcmd")  # noqa: SLF001
    page._save_command()  # noqa: SLF001
    assert h.config.command == "tts"
    assert "addcmd" in page._command_msg.text()  # noqa: SLF001
    page._command.setText("!Leer")  # noqa: SLF001
    page._save_command()  # noqa: SLF001
    assert h.config.command == "leer"
    assert page._command.text() == "leer"  # noqa: SLF001


def test_commands_page_describes_renamed_command(h):
    from alpekabot.ui.commands_tab import BuiltinRow
    from PySide6.QtWidgets import QLabel

    h.svc.set_command("voz")
    info = {"voz": ("mensaje", "Lee en voz alta.", "TTS")}
    row = BuiltinRow("voz", Permission.EVERYONE, info)
    texts = [w.text() for w in row.findChildren(QLabel)]
    assert "Lee en voz alta." in texts
    assert "Comando interno." not in texts


def test_page_creation_saves_nothing(h):
    from alpekabot.ui.tts_tab import TtsTab

    TtsTab(h.svc, h.bot.bus)
    assert h.saves == 0


def test_page_viewer_text_is_not_html(h):
    from PySide6.QtCore import Qt

    from alpekabot.ui.commands_tab import ElidedLabel
    from alpekabot.ui.tts_tab import QueueRow, TtsTab

    h.run(msg("!tts <img src=x>hola"))
    page = TtsTab(h.svc, h.bot.bus)
    rows = [page._queue_list.itemAt(i).widget() for i in range(page._queue_list.count())]  # noqa: SLF001
    row = next(r for r in rows if isinstance(r, QueueRow))
    labels = row.findChildren(ElidedLabel)
    assert labels and all(lbl.textFormat() == Qt.PlainText for lbl in labels)


def test_page_keeps_unplugged_device(bot, db):
    from alpekabot.ui.tts_tab import TtsTab

    h = Harness(bot, db, output_device="desconectado")
    page = TtsTab(h.svc, bot.bus)
    assert page._device.currentData() == "desconectado"  # noqa: SLF001
    assert h.config.output_device == "desconectado"


def test_page_loads_voices_and_saves_choice(h):
    from alpekabot.ui.tts_tab import TtsTab

    async def voices():
        return [Voice("es-ES-ElviraNeural", "es-ES · Elvira"), Voice("es-MX-DaliaNeural", "es-MX · Dalia")]

    h.svc._voices = voices  # noqa: SLF001
    page = TtsTab(h.svc, h.bot.bus)
    asyncio.run(page._load_voices())  # noqa: SLF001
    assert page._voice.currentData() == "es-MX-DaliaNeural"  # noqa: SLF001
    page._voice.setCurrentIndex(0)  # noqa: SLF001
    assert h.config.voice == "es-ES-ElviraNeural"


def test_page_survives_voice_list_failure(h):
    from alpekabot.ui.tts_tab import TtsTab

    async def voices():
        raise OSError("sin internet")

    h.svc._voices = voices  # noqa: SLF001
    page = TtsTab(h.svc, h.bot.bus)
    asyncio.run(page._load_voices())  # noqa: SLF001
    assert page._voice.currentData() == "es-MX-DaliaNeural"  # noqa: SLF001
    assert "internet" in page._voice_msg.text()  # noqa: SLF001
