"""Configuración: tres pestañas (Cuentas, General y Actualizaciones), Cuentas separada por red y lo avanzado de Kick plegado."""

import tempfile
from pathlib import Path

from PySide6.QtWidgets import QTabWidget, QToolButton

from alpekabot.bot import AlpekaBot
from alpekabot.config import AppConfig
from alpekabot.ui.settings_dialog import SettingsDialog


def _dialog(use_own_app: bool = False) -> SettingsDialog:
    config = AppConfig()
    config.kick.use_own_app = use_own_app
    dlg = SettingsDialog(AlpekaBot(config, db_path=Path(tempfile.mkdtemp()) / "t.db"))
    dlg.show()
    return dlg


def test_dos_pestanas_y_las_cuatro_cuentas_juntas(qt_app):
    dlg = _dialog()
    tabs = dlg.findChildren(QTabWidget)[0]
    assert [tabs.tabText(i) for i in range(tabs.count())] == ["Cuentas", "General", "Actualizaciones"]
    assert len(dlg._rows) == 4  # bot y streamer de Twitch y de Kick, en la misma pestaña


def test_cuentas_se_separa_por_red_con_un_selector(qt_app):
    dlg = _dialog()
    twitch, kick = dlg._network_pages["twitch"], dlg._network_pages["kick"]
    assert dlg._network.value() == "twitch" and twitch.isVisible() and not kick.isVisible()
    dlg._network._buttons["kick"].click()
    assert dlg._network.value() == "kick" and kick.isVisible() and not twitch.isVisible()
    dlg._network._buttons["twitch"].click()
    assert twitch.isVisible() and not kick.isVisible()


def test_lo_avanzado_de_kick_esta_plegado_hasta_abrirlo(qt_app):
    dlg = _dialog()
    dlg._network._buttons["kick"].click()  # Kick está en su propia vista
    fold = next(b for b in dlg.findChildren(QToolButton) if b.text() == "Avanzado")
    assert not dlg._kick_own.isVisible()
    fold.click()
    assert dlg._kick_own.isVisible()


def test_con_app_propia_lo_avanzado_empieza_abierto(qt_app):
    dlg = _dialog(use_own_app=True)
    dlg._network._buttons["kick"].click()
    assert dlg._kick_own.isVisible()
