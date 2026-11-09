"""Página «VTube Studio» (sin pantalla): estado, reacciones y su editor, con los premios."""

import asyncio

import pytest
from PySide6.QtWidgets import QLabel

from alpekabot.bot import AlpekaBot
from alpekabot.config import AppConfig
from alpekabot.core.models import ConnectionState, EventKind, Platform, Reward
from alpekabot.features.reactions import EXPRESSION, HOTKEY, WAIT, Reaction, RewardLink, Step
from alpekabot.integrations.vtube_studio import Expression, Hotkey, VTSError, VTSState, VTSStatus
from alpekabot.platforms.base import PlatformError
from alpekabot.ui.vtube_tab import ReactionDialog, RewardDialog, VTubeTab, steps_text, trigger_text

TW = frozenset({Platform.TWITCH})


class FakeVTube:
    def __init__(self, bus, connected=True):
        self.bus = bus
        self.status = VTSStatus(VTSState.CONNECTED if connected else VTSState.OFF, model="Alpeka" if connected else "")
        self.calls = []
        self.authorize_error = None
        self.restarts = 0

    @property
    def is_connected(self):
        return self.status.state is VTSState.CONNECTED

    def set_status(self, status):
        self.status = status
        self.bus.vts_status.emit(status)

    async def restart(self):
        self.restarts += 1

    async def stop(self):
        pass

    async def authorize(self):
        if self.authorize_error:
            raise self.authorize_error
        self.set_status(VTSStatus(VTSState.CONNECTED, model="Alpeka"))

    async def hotkeys(self):
        return [Hotkey("h1", "Saludo"), Hotkey("h2", "Baile")]

    async def expressions(self):
        return [Expression("risa.exp3.json", "Risa")]

    async def trigger_hotkey(self, hotkey_id):
        self.calls.append(hotkey_id)

    async def set_expression(self, file, active, fade_s=0.25):
        self.calls.append((file, active))


class FakePlatform:
    def __init__(self, platform, rewards=(), connected=True):
        self.platform = platform
        self.is_connected = connected
        self._rewards = list(rewards)
        self.created = []
        self.deleted = []
        self.state = ConnectionState.CONNECTED if connected else ConnectionState.DISCONNECTED
        self.error = None

    async def list_rewards(self):
        if self.error:
            raise self.error
        return list(self._rewards)

    async def create_reward(self, title, cost, prompt=""):
        if self.error:
            raise self.error
        self.created.append((title, cost, prompt))
        reward = Reward(self.platform, f"id-{title}", title, cost, True)
        self._rewards.append(reward)
        return reward

    async def delete_reward(self, reward_id):
        if self.error:
            raise self.error
        self.deleted.append(reward_id)
        self._rewards = [r for r in self._rewards if r.id != reward_id]


@pytest.fixture
def bot():
    b = AlpekaBot(AppConfig(), db_path=":memory:")
    fake = FakeVTube(b.bus)
    b.vtube = fake
    b.reactions._avatar = fake
    b.platforms[Platform.TWITCH] = FakePlatform(Platform.TWITCH, [Reward(Platform.TWITCH, "r1", "Saludo", 100)])
    b.platforms[Platform.KICK] = FakePlatform(Platform.KICK, connected=False)
    yield b
    b.db.close()


async def settle():
    for _ in range(20):
        await asyncio.sleep(0)


def run(coro):
    return asyncio.run(coro)


# -------------------------------------------------------------------- textos
def test_textos():
    tw, kk = RewardLink(Platform.TWITCH, "t", "Saludo"), RewardLink(Platform.KICK, "k", "Hola")
    assert trigger_text(Reaction("a", EventKind.REWARD, rewards=(tw,))) == "Canje de «Saludo»"
    assert trigger_text(Reaction("a", EventKind.REWARD, rewards=(tw, kk))) == "Canje: Twitch «Saludo» · Kick «Hola»"
    assert trigger_text(Reaction("a", EventKind.GIFT, min_amount=5)) == "Subs regaladas: 5 o más"
    assert trigger_text(Reaction("a", EventKind.GIFT)) == "Subs regaladas"
    assert trigger_text(Reaction("a", EventKind.FOLLOW)) == "Follow"
    steps = (Step(HOTKEY, "h", "Saludo"), Step(WAIT, seconds=1.5), Step(EXPRESSION, "e", "Risa", 2, True))
    assert steps_text(Reaction("a", EventKind.FOLLOW, steps)) == \
        "Hotkey «Saludo» → Esperar 1.5 s → Expresión «Risa» durante 2 s"
    assert steps_text(Reaction("a", EventKind.FOLLOW, message="x")) == "Solo mensaje al chat"


# -------------------------------------------------------------------- estado
def test_boton_autorizar_solo_cuando_hace_falta(bot):
    async def main():
        page = VTubeTab(bot)
        assert page.auth_button.isHidden()
        bot.vtube.set_status(VTSStatus(VTSState.NEEDS_AUTH, "pulsa"))
        assert not page.auth_button.isHidden()
        await page._authorize()
        assert page.auth_button.isHidden() and bot.vtube.is_connected
        assert page.auth_button.isEnabled() and page.auth_button.text() == "Autorizar"
    run(main())


def test_autorizar_rechazado_se_muestra(bot):
    async def main():
        page = VTubeTab(bot)
        bot.vtube.set_status(VTSStatus(VTSState.NEEDS_AUTH))
        bot.vtube.authorize_error = VTSError("Rechazaste el permiso en VTube Studio.")
        await page._authorize()
        assert "Rechazaste" in page.notice.text() and not page.notice.isHidden()
        assert page.auth_button.isEnabled()
    run(main())


def test_activar_y_cambiar_puerto_se_aplican(bot):
    async def main():
        page = VTubeTab(bot)
        page.enabled_toggle.setChecked(True)
        await settle()
        assert bot.config.vtube.enabled and bot.vtube.restarts == 1
        page.port.setValue(9000)
        page.port.editingFinished.emit()
        await settle()
        assert bot.config.vtube.port == 9000 and bot.vtube.restarts == 2
        page.port.editingFinished.emit()  # sin cambios: no reinicia
        await settle()
        assert bot.vtube.restarts == 2
    run(main())


def test_cambios_durante_la_aplicacion_no_se_pierden(bot):
    async def main():
        page = VTubeTab(bot)
        page.enabled_toggle.setChecked(True)
        page.enabled_toggle.setChecked(False)
        page.enabled_toggle.setChecked(True)
        await settle()
        assert bot.config.vtube.enabled is True
    run(main())


# ---------------------------------------------------------------- reacciones
def test_lista_se_refresca_con_la_senal(bot):
    async def main():
        page = VTubeTab(bot)
        assert not page._reactions_empty.isHidden() and page._reactions.count() == 0
        bot.reactions.repo.add(Reaction("<b>Hola</b>", EventKind.FOLLOW, (Step(WAIT, seconds=1),)))
        assert page._reactions_empty.isHidden() and page._reactions.count() == 1
        assert page._reactions_count.text() == "1 reacción"
    run(main())


def test_fila_no_se_ensancha_con_textos_largos(bot):
    """Con la ventana al mínimo (página ≈ 750 px), los botones de la fila deben seguir visibles."""
    async def main():
        page = VTubeTab(bot)
        steps = tuple(Step(HOTKEY, f"h{i}", "Un nombre de hotkey muy largo") for i in range(5))
        bot.reactions.repo.add(Reaction("N" * 40, EventKind.GIFT, steps, min_amount=1000, cooldown_s=86_400,
                                        message="x"))
        row = page._reactions.itemAt(0).widget()
        assert row.minimumSizeHint().width() < 750
    run(main())


def test_probar_desde_la_lista(bot):
    async def main():
        bot.reactions.start()
        page = VTubeTab(bot)
        r = bot.reactions.repo.add(Reaction("a", EventKind.FOLLOW, (Step(HOTKEY, "h1", "Saludo"),), message="x"))
        page.test_reaction(r)
        await settle()
        assert bot.vtube.calls == ["h1"] and "Probando" in page.notice.text()
        await bot.reactions.stop()
    run(main())


def test_probar_explica_por_que_no_se_puede(bot):
    async def main():
        page = VTubeTab(bot)
        page.test_reaction(Reaction("a", EventKind.FOLLOW, message="x"))
        assert "no tiene pasos" in page.notice.text()
        bot.vtube.set_status(VTSStatus(VTSState.OFF))
        page.test_reaction(Reaction("a", EventKind.FOLLOW, (Step(HOTKEY, "h1"),)))
        assert "conectado" in page.notice.text()
    run(main())


# ------------------------------------------------------------------- editor
def test_crear_regalo_con_minimo_y_pasos(bot):
    async def main():
        dlg = ReactionDialog(bot, None)
        await settle()
        dlg.name.setText("Regalo")
        dlg.trigger.setCurrentIndex(dlg.trigger.findData(EventKind.GIFT.value))
        assert not dlg._min_box.isHidden() and dlg._reward_box.isHidden() and not dlg._platforms_box.isHidden()
        dlg.min_amount.setValue(5)
        dlg.new_step(HOTKEY)
        dlg.rows[0].combo.setCurrentIndex(dlg.rows[0].combo.findData("h2"))
        dlg.new_step(EXPRESSION)
        dlg.rows[1].seconds.setValue(3)
        dlg.new_step(WAIT)
        dlg.message.setPlainText("Gracias\n$user")
        dlg.cooldown.setValue(10)
        dlg.save()
        got = bot.reactions.repo.list_all()[0]
        assert got.trigger is EventKind.GIFT and got.min_amount == 5 and got.cooldown_s == 10
        assert got.steps == (Step(HOTKEY, "h2", "Baile"), Step(EXPRESSION, "risa.exp3.json", "Risa", 3, True),
                             Step(WAIT, seconds=1.0))
        assert got.message == "Gracias $user"
    run(main())


def test_premio_decide_la_plataforma(bot):
    bot.platforms[Platform.KICK] = FakePlatform(Platform.KICK, [Reward(Platform.KICK, "k1", "En Kick", 50)])

    async def main():
        dlg = ReactionDialog(bot, None)
        await settle()
        assert dlg._platforms_box.isHidden() and not dlg._reward_box.isHidden()
        dlg.name.setText("Canje")
        dlg.pickers[Platform.KICK].select("k1")
        dlg.new_step(WAIT)
        dlg.save()
        got = bot.reactions.repo.list_all()[0]
        assert got.rewards == (RewardLink(Platform.KICK, "k1", "En Kick"),)
        assert got.platforms == frozenset({Platform.KICK})
    run(main())


def test_canje_con_un_premio_en_cada_plataforma(bot):
    bot.platforms[Platform.KICK] = FakePlatform(Platform.KICK, [Reward(Platform.KICK, "k1", "En Kick", 50)])

    async def main():
        dlg = ReactionDialog(bot, None)
        await settle()
        dlg.name.setText("Canje")
        dlg.pickers[Platform.TWITCH].select("r1")
        dlg.pickers[Platform.KICK].select("k1")
        dlg.new_step(WAIT)
        dlg.save()
        saved = bot.reactions.repo.list_all()[0]
        assert saved.rewards == (RewardLink(Platform.TWITCH, "r1", "Saludo"), RewardLink(Platform.KICK, "k1", "En Kick"))
        assert saved.platforms == frozenset(Platform)

        # Al editarla se recuperan los dos, y quitar uno deja solo la otra plataforma
        again = ReactionDialog(bot, saved)
        await settle()
        assert again.value() == saved
        again.pickers[Platform.TWITCH].select("")
        again.save()
        got = bot.reactions.repo.list_all()[0]
        assert got.rewards == (RewardLink(Platform.KICK, "k1", "En Kick"),)
        assert got.platforms == frozenset({Platform.KICK})
    run(main())


def test_el_panel_no_muestra_premios(bot):
    async def main():
        page = VTubeTab(bot)
        page.show()
        await settle()
        assert not hasattr(page, "_rewards") and not hasattr(page, "rewards")
        assert all(lbl.text() != "Premios de puntos de canal" for lbl in page.findChildren(QLabel))
        page.hide()
    run(main())


def test_crear_premio_desde_el_editor_lo_selecciona(bot):
    async def main():
        dlg = ReactionDialog(bot, None)
        await settle()
        picker = dlg.pickers[Platform.TWITCH]
        assert picker.delete_button.isHidden()
        rd = dlg.open_reward_dialog(Platform.TWITCH)
        assert rd.platform is Platform.TWITCH
        await rd.submit()
        assert "título" in rd.error.text()
        rd.title_edit.setText("Baile")
        rd.cost.setValue(500)
        rd.prompt.setText("Baila")
        await rd.submit()
        assert bot.platforms[Platform.TWITCH].created == [("Baile", 500, "Baila")]
        assert bot.reactions.repo.is_managed(Platform.TWITCH, "id-Baile")
        assert picker.selected().id == "id-Baile" and not picker.delete_button.isHidden()
    run(main())


def test_crear_premio_con_error(bot):
    bot.platforms[Platform.TWITCH].error = PlatformError("Twitch no dejó crear el premio")

    async def main():
        dlg = RewardDialog(bot, Platform.TWITCH)
        dlg.title_edit.setText("Baile")
        await dlg.submit()
        assert "no dejó" in dlg.error.text() and dlg._create.isEnabled()
        assert bot.reactions.repo.managed_ids(Platform.TWITCH) == set()
    run(main())


def test_no_se_crean_premios_en_una_plataforma_sin_conectar(bot):
    async def main():
        dlg = ReactionDialog(bot, None)
        await settle()
        assert dlg.pickers[Platform.TWITCH].create_button.isEnabled()
        assert not dlg.pickers[Platform.KICK].create_button.isEnabled()
    run(main())


def test_borrar_premio_solo_los_de_la_app(bot):
    async def main():
        bot.reactions.repo.add_managed_reward(Platform.TWITCH, "r1", "Saludo")
        bot.platforms[Platform.TWITCH]._rewards.append(Reward(Platform.TWITCH, "r2", "Ajeno", 5))
        dlg = ReactionDialog(bot, None)
        await settle()
        picker = dlg.pickers[Platform.TWITCH]
        picker.select("r2")
        assert picker.delete_button.isHidden()
        picker.select("r1")
        assert not picker.delete_button.isHidden()
        await dlg.delete_reward(picker, picker.selected())
        assert bot.platforms[Platform.TWITCH].deleted == ["r1"]
        assert not bot.reactions.repo.is_managed(Platform.TWITCH, "r1")
        assert picker.selected() is None and picker.combo.findData("r1") == -1
    run(main())


def test_borrar_premio_con_error_lo_mantiene(bot):
    async def main():
        bot.reactions.repo.add_managed_reward(Platform.TWITCH, "r1", "Saludo")
        dlg = ReactionDialog(bot, None)
        await settle()
        picker = dlg.pickers[Platform.TWITCH]
        picker.select("r1")
        bot.platforms[Platform.TWITCH].error = PlatformError("Twitch dijo que no")
        await dlg.delete_reward(picker, picker.selected())
        assert "dijo que no" in dlg._reward_hint.text() and picker.selected().id == "r1"
        assert bot.reactions.repo.is_managed(Platform.TWITCH, "r1")
    run(main())


def test_sin_premio_muestra_el_error_de_premio(bot):
    bot.platforms[Platform.TWITCH] = FakePlatform(Platform.TWITCH)

    async def main():
        dlg = ReactionDialog(bot, None)
        await settle()
        dlg.name.setText("Canje")
        dlg.new_step(WAIT)
        dlg.save()
        assert "premio" in dlg.error.text() and bot.reactions.repo.list_all() == []
    run(main())


def test_premio_guardado_se_conserva_si_no_se_puede_listar(bot):
    bot.platforms[Platform.TWITCH] = FakePlatform(Platform.TWITCH, connected=False)
    saved = bot.reactions.repo.add(Reaction("Canje", EventKind.REWARD, (Step(WAIT, seconds=1),), platforms=TW,
                                            rewards=(RewardLink(Platform.TWITCH, "r9", "Viejo"),)))

    async def main():
        dlg = ReactionDialog(bot, saved)
        await settle()
        assert "Conecta" in dlg._reward_hint.text()
        assert dlg.value() == saved
    run(main())


def test_error_al_listar_premios_se_muestra(bot):
    bot.platforms[Platform.TWITCH].error = PlatformError("hace falta ser afiliado")

    async def main():
        dlg = ReactionDialog(bot, None)
        await settle()
        assert "afiliado" in dlg._reward_hint.text()
    run(main())


def test_raid_solo_twitch(bot):
    async def main():
        dlg = ReactionDialog(bot, None)
        await settle()
        dlg.name.setText("Raid")
        dlg.trigger.setCurrentIndex(dlg.trigger.findData(EventKind.RAID.value))
        assert dlg._platforms_box.isHidden()
        dlg.min_amount.setValue(10)
        dlg.new_step(WAIT)
        dlg.save()
        got = bot.reactions.repo.list_all()[0]
        assert got.platforms == TW and got.min_amount == 10
    run(main())


def test_el_minimo_no_se_guarda_donde_no_aplica(bot):
    async def main():
        dlg = ReactionDialog(bot, None)
        await settle()
        dlg.name.setText("Follow")
        dlg.trigger.setCurrentIndex(dlg.trigger.findData(EventKind.BITS.value))
        dlg.min_amount.setValue(100)
        dlg.trigger.setCurrentIndex(dlg.trigger.findData(EventKind.FOLLOW.value))
        dlg.new_step(WAIT)
        dlg.save()
        assert bot.reactions.repo.list_all()[0].min_amount == 0
    run(main())


def test_vts_cerrado_no_deja_elegir_pero_conserva_los_pasos(bot):
    bot.vtube.status = VTSStatus(VTSState.OFF)
    step = Step(HOTKEY, "h7", "Antigua")
    saved = bot.reactions.repo.add(Reaction("a", EventKind.FOLLOW, (step,)))

    async def main():
        dlg = ReactionDialog(bot, saved)
        await settle()
        assert not dlg.add_hotkey.isEnabled() and not dlg.add_expression.isEnabled() and dlg.add_wait.isEnabled()
        assert "no está conectado" in dlg._steps_hint.text()
        assert dlg.rows[0].value() == step
        assert "no disponible" in dlg.rows[0].combo.currentText()
    run(main())


def test_hotkey_de_otro_modelo_se_conserva_con_vts_abierto(bot):
    saved = bot.reactions.repo.add(Reaction("a", EventKind.FOLLOW, (Step(HOTKEY, "h7", "Antigua"),)))

    async def main():
        dlg = ReactionDialog(bot, saved)
        await settle()
        assert dlg.rows[0].value() == Step(HOTKEY, "h7", "Antigua")
        assert dlg.rows[0].combo.count() == 3
    run(main())


def test_expresion_quitar_no_tiene_duracion(bot):
    async def main():
        dlg = ReactionDialog(bot, None)
        await settle()
        dlg.new_step(EXPRESSION)
        row = dlg.rows[0]
        row.seconds.setValue(5)
        row.mode.setCurrentIndex(1)
        assert not row.seconds.isEnabled()
        assert row.value() == Step(EXPRESSION, "risa.exp3.json", "Risa", 0, False)
    run(main())


def test_mover_y_quitar_pasos(bot):
    async def main():
        dlg = ReactionDialog(bot, None)
        await settle()
        dlg.new_step(HOTKEY)
        dlg.new_step(WAIT)
        dlg.new_step(EXPRESSION)
        first, second, third = dlg.rows
        dlg.move_step(third, -1)
        assert dlg.rows == [first, third, second]
        assert [dlg._steps.itemAt(i).widget() for i in range(3)] == [first, third, second]
        dlg.move_step(first, -1)  # ya es el primero
        dlg.move_step(second, 1)  # ya es el último
        assert dlg.rows == [first, third, second]
        dlg.remove_step(third)
        assert dlg.rows == [first, second] and dlg._steps.count() == 2
    run(main())


def test_editar_conserva_el_id(bot):
    saved = bot.reactions.repo.add(Reaction("a", EventKind.FOLLOW, (Step(WAIT, seconds=2),), cooldown_s=7))

    async def main():
        dlg = ReactionDialog(bot, saved)
        await settle()
        assert dlg.value() == saved
        dlg.name.setText("b")
        dlg.save()
        assert [r.name for r in bot.reactions.repo.list_all()] == ["b"]
    run(main())


def test_probar_desde_el_editor_sin_guardar(bot):
    async def main():
        bot.reactions.start()
        dlg = ReactionDialog(bot, None)
        await settle()
        dlg.trigger.setCurrentIndex(dlg.trigger.findData(EventKind.FOLLOW.value))
        dlg.new_step(HOTKEY)
        dlg.test()
        await settle()
        assert bot.vtube.calls == ["h1"] and bot.reactions.repo.list_all() == []
        await bot.reactions.stop()
    run(main())


def test_probar_con_paso_vacio_avisa(bot):
    async def main():
        dlg = ReactionDialog(bot, None)
        dlg._add_row(Step(HOTKEY))  # p. ej. si el modelo no tiene hotkeys
        dlg.rows[0].set_options([])
        dlg.test()
        assert "sin hotkey" in dlg.error.text() and bot.vtube.calls == []
    run(main())


def test_cerrar_el_editor_cancela_sus_tareas(bot):
    async def main():
        dlg = ReactionDialog(bot, None)
        tasks = list(dlg._tasks)
        dlg.reject()
        await settle()
        assert all(task.done() for task in tasks)
    run(main())
