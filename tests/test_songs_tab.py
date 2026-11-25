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


def test_botones_anterior_aleatorio_y_repetir(page):
    svc = page.svc
    assert not page._prev.isEnabled()
    svc.repo.backup_add(info(1))
    svc.repo.backup_add(info(2))
    svc.play()
    svc.skip()
    assert page._prev.isEnabled()
    page._shuffle.click()
    page._repeat.click()
    assert svc.config.shuffle is True and page._status.repeat is True
    assert "activado" in page._shuffle.toolTip()


def test_play_en_la_fila_del_respaldo(page):
    svc = page.svc
    svc.repo.backup_add(info(1))
    svc.repo.backup_add(info(2))
    page.reload()
    row = page._backup_list.itemAt(1).widget()
    from PySide6.QtWidgets import QPushButton
    play = [b for b in row.findChildren(QPushButton) if b.toolTip() == "Reproducir ya"][0]
    play.click()
    assert svc.status.current.video_id == info(2).video_id


def test_pestanas_cola_y_respaldo_con_contadores(page):
    svc = page.svc
    for n in (1, 2, 3):
        svc.repo.backup_add(info(n))
    assert not page._queue_page.isHidden() and page._backup_page.isHidden()
    assert "(3)" in page._tabs._buttons["backup"].text() and "(0)" in page._tabs._buttons["queue"].text()
    page._tabs._buttons["backup"].click()
    assert page._queue_page.isHidden() and not page._backup_page.isHidden()


def test_buscador_y_resaltado_del_respaldo(page):
    from alpekabot.ui.songs_tab import BackupRow
    svc = page.svc
    for n in (1, 2, 3):
        svc.repo.backup_add(info(n))
    rows = lambda: [page._backup_list.itemAt(i).widget() for i in range(page._backup_list.count())]
    page._backup_filter.setText("canción 2")
    assert len(rows()) == 1 and not page._backup_empty.isVisible()
    page._backup_filter.setText("no existe")
    assert rows() == [] and page._backup_empty.text() == "No hay coincidencias."
    page._backup_filter.setText("")
    svc.play_backup(svc.repo.backup_list()[1].id)
    styled = [r for r in rows() if isinstance(r, BackupRow) and r.styleSheet()]
    assert len(styled) == 1 and styled is not None


def test_portada_en_el_cuadro_del_disco(page, monkeypatch):
    from PySide6.QtGui import QPixmap
    from alpekabot.ui.songs_tab import Thumbnails

    vid = "abcdefghijk"
    cover = QPixmap(320, 180)
    cover.fill(Qt.red)
    monkeypatch.setitem(Thumbnails._raw, vid, cover)  # sin pedirla a internet
    disc = page._disc.pixmap().toImage()
    page.svc.repo.backup_add(VideoInfo(vid, "Con portada", "Canal", "UC", 180))
    page.svc.play()
    assert page._disc.property("thumb_id") == vid
    assert page._disc.pixmap().toImage().pixelColor(55, 55) == Qt.red
    page.svc.stop()  # sin canción: vuelve el disco
    assert page._disc.property("thumb_id") == ""
    assert page._disc.pixmap().toImage() == disc
