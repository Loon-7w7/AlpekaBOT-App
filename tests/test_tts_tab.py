"""Página «Texto a voz» (sin pantalla): modos, aviso de «todo el chat» y premios del modo canje."""

import asyncio

import pytest
from PySide6.QtWidgets import QMessageBox

from alpekabot.bot import AlpekaBot
from alpekabot.config import AppConfig
from alpekabot.core.models import ChannelEvent, EventKind, Platform, Reward
from alpekabot.ui import tts_tab as tts_tab_module
from alpekabot.ui.tts_tab import TtsTab
from test_vtube_tab import FakePlatform


class TtsPlatform(FakePlatform):
    async def create_reward(self, title, cost, prompt="", input_required=False):
        reward = await super().create_reward(title, cost, prompt)
        self.created[-1] = (title, cost, prompt, input_required)
        return reward


@pytest.fixture
def bot():
    b = AlpekaBot(AppConfig(), db_path=":memory:")
    b.platforms[Platform.TWITCH] = TtsPlatform(Platform.TWITCH, [Reward(Platform.TWITCH, "r1", "Hablar", 500)])
    b.platforms[Platform.KICK] = TtsPlatform(Platform.KICK, connected=False)
    yield b
    b.db.close()


def answer(monkeypatch, button):
    monkeypatch.setattr(tts_tab_module.QMessageBox, "warning", lambda *a, **k: button)


def test_modo_comando_por_defecto(bot):
    page = TtsTab(bot.tts, bot.bus, bot)
    assert page._mode.currentData() == "command"
    assert not page._command_box.isHidden()
    assert page._chat_warning.isHidden() and page._rewards_box.isHidden()


def test_todo_el_chat_pide_confirmacion(bot, monkeypatch):
    page = TtsTab(bot.tts, bot.bus, bot)
    answer(monkeypatch, QMessageBox.Cancel)
    page._mode.setCurrentIndex(page._mode.findData("chat"))
    assert bot.config.tts.mode == "command"
    assert page._mode.currentData() == "command"
    answer(monkeypatch, QMessageBox.Yes)
    page._mode.setCurrentIndex(page._mode.findData("chat"))
    assert bot.config.tts.mode == "chat"
    assert not page._chat_warning.isHidden() and page._command_box.isHidden()


def test_modo_canje_elige_y_crea_premios(bot):
    async def main():
        page = TtsTab(bot.tts, bot.bus, bot)
        page._mode.setCurrentIndex(page._mode.findData("reward"))
        await page.load_rewards()
        assert not page._rewards_box.isHidden() and page._cooldown_box.isHidden()
        picker = page.pickers[Platform.TWITCH]
        picker.select("r1")
        assert bot.config.tts.rewards == [{"platform": "twitch", "id": "r1", "title": "Hablar"}]
        dlg = page.open_reward_dialog(Platform.TWITCH)
        dlg.title_edit.setText("TTS")
        await dlg.submit()
        assert bot.platforms[Platform.TWITCH].created == [("TTS", 100, "", True)]
        assert bot.config.tts.rewards[0]["id"] == "id-TTS"
        assert bot.reactions.repo.is_managed(Platform.TWITCH, "id-TTS")
        assert not picker.delete_button.isHidden()

    asyncio.run(main())


def test_canje_de_premio_de_la_app_se_devuelve_si_tts_apagado(bot):
    calls = []

    async def complete(reward_id, redemption_id, refund):
        calls.append((redemption_id, refund))

    bot.platforms[Platform.TWITCH].complete_redemption = complete
    bot.reactions.repo.add_managed_reward(Platform.TWITCH, "id-TTS", "TTS")
    bot.tts.set_reward(Platform.TWITCH, "id-TTS", "TTS")

    async def main():
        bot.bus.channel_event.emit(ChannelEvent(Platform.TWITCH, EventKind.REWARD, "u1", "Pepe", 100, "id-TTS",
                                                "TTS", "c1", "hola", pending=True))
        for _ in range(10):
            await asyncio.sleep(0)

    asyncio.run(main())
    assert calls == [("c1", True)]  # el TTS empieza desactivado


def test_pagina_sin_bot_no_falla(bot):
    page = TtsTab(bot.tts, bot.bus)
    bot.tts.update(mode="reward")
    assert "no están disponibles" in page._reward_hint.text()
