"""Página Canciones: estrella de favoritos y barra para adelantar."""

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget

from alpekabot.config import SongRequestConfig
from alpekabot.core.models import Platform
from alpekabot.features.songs import SongRepository, SongService
from alpekabot.features.youtube import VideoInfo
from alpekabot.ui.songs_tab import QueueRow, SeekBar, SongsTab


class FakePlayer:
    def __init__(self):
        self.view = QWidget()
        self.seeks: list[float] = []
        self.on_ended = self.on_error = self.on_playing = None

    def load(self, vid, vol): pass
    def pause(self): pass
    def resume(self): pass
    def stop(self): pass
    def set_volume(self, v): pass
    def seek(self, s): self.seeks.append(s)


def info(n: int) -> VideoInfo:
    return VideoInfo(f"vid{n:08d}xx", f"Canción {n}", "Canal", "UC", 180)


@pytest.fixture
def player():
    return FakePlayer()


@pytest.fixture
def page(db, bot, player):
    svc = SongService(bot.bus, SongRepository(db, bot.bus), SongRequestConfig(), None)
    svc.attach_player(player)
    return SongsTab(svc, bot.bus, player)


def test_estrella_oculta_sin_cancion_y_visible_al_sonar(page):
    assert page._fav_btn.isHidden()
    page.svc.repo.backup_add(info(1))
    page.svc.play()
    assert not page._fav_btn.isHidden()
    page.svc.stop()
    assert page._fav_btn.isHidden()


def test_estrella_de_la_cancion_actual_alterna_el_respaldo(page):
    page.svc.repo.backup_add(info(1))
    page.svc.repo.backup_add(info(2))
    page.svc.play()
    assert "Quitar" in page._fav_btn.toolTip()
    page._fav_btn.click()
    assert [s.title for s in page.svc.repo.backup_list()] == ["Canción 2"]
    assert page.svc.status.current.title == "Canción 1"  # sigue sonando
    assert "Añadir" in page._fav_btn.toolTip()
    page._fav_btn.click()
    assert [s.title for s in page.svc.repo.backup_list()] == ["Canción 2", "Canción 1"]


def test_estrella_en_las_filas_de_la_cola(page):
    page.svc.repo.queue_add(info(1), "ana", "1", Platform.TWITCH)
    row = page.findChildren(QueueRow)[-1]
    assert "Añadir" in row.fav_btn.toolTip()
    row.fav_btn.click()
    assert [s.title for s in page.svc.repo.backup_list()] == ["Canción 1"]
    assert "Quitar" in page.findChildren(QueueRow)[-1].fav_btn.toolTip()  # la fila se rehízo


def test_barra_clic_adelanta_a_esa_posicion(page, player):
    page.svc.repo.backup_add(info(1))
    page.svc.play()
    page.set_progress(10, 200)
    page._bar.resize(1000, 18)
    QTest.mouseClick(page._bar, Qt.LeftButton, pos=QPoint(500, 9))
    assert player.seeks == [pytest.approx(100, abs=1)]
    assert page._pos == pytest.approx(100, abs=1)


def test_barra_sin_duracion_no_adelanta(page, player):
    page.svc.repo.backup_add(info(1))
    page.svc.play()
    page.set_progress(0, 0)
    page._bar.resize(1000, 18)
    QTest.mouseClick(page._bar, Qt.LeftButton, pos=QPoint(500, 9))
    assert player.seeks == []


def test_arrastrar_previsualiza_y_el_progreso_no_pisa_la_barra():
    bar = SeekBar()
    bar.resize(1000, 18)
    bar.set_seekable(True)
    bar.set_value(0.1)
    sent, previews = [], []
    bar.seeked.connect(sent.append)
    bar.scrubbed.connect(previews.append)
    QTest.mousePress(bar, Qt.LeftButton, pos=QPoint(500, 9))
    QTest.mouseMove(bar, QPoint(800, 9))
    assert previews and previews[-1] == pytest.approx(0.8, abs=0.01)
    assert sent == []
    bar.set_value(0.2)  # llega el progreso real mientras arrastras: se ignora
    assert bar.value == pytest.approx(0.8, abs=0.01)
    QTest.mouseRelease(bar, Qt.LeftButton, pos=QPoint(800, 9))
    assert sent == [pytest.approx(0.8, abs=0.01)]
    bar.set_value(0.2)
    assert bar.value == pytest.approx(0.2)
