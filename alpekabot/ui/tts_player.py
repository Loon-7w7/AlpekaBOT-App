"""Reproduce el audio del TTS por el dispositivo elegido. Implementa ``features.tts.TtsPlayer``."""

from __future__ import annotations

import asyncio
import logging

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QUrl
from PySide6.QtMultimedia import QAudioDevice, QAudioOutput, QMediaDevices, QMediaPlayer

log = logging.getLogger(__name__)

# Ningún mensaje de 500 caracteres dura tanto: evita que la cola se quede atascada
MAX_PLAY_S = 120


def device_id(device: QAudioDevice) -> str:
    return bytes(device.id().data()).decode("utf-8", "replace")


def output_devices() -> list[tuple[str, str]]:
    """Salidas de audio de Windows: [(id, nombre)]."""
    return [(device_id(d), d.description()) for d in QMediaDevices.audioOutputs()]


def _find_output(wanted: str) -> QAudioDevice:
    if wanted:
        for d in QMediaDevices.audioOutputs():
            if device_id(d) == wanted:
                return d
        log.warning("TTS: la salida de audio elegida no está disponible; se usa la predeterminada")
    return QMediaDevices.defaultAudioOutput()


class QtTtsPlayer(QObject):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._output = QAudioOutput(self)
        self._player = QMediaPlayer(self)
        self._player.setAudioOutput(self._output)
        self._player.mediaStatusChanged.connect(self._on_status)
        self._player.errorOccurred.connect(self._on_error)
        self._done: asyncio.Future | None = None

    def set_volume(self, volume: int) -> None:
        self._output.setVolume(max(0, min(100, volume)) / 100)

    async def play(self, audio: bytes, volume: int, device: str) -> None:
        buffer = QBuffer(self)
        buffer.setData(QByteArray(audio))
        buffer.open(QIODevice.ReadOnly)
        self._done = asyncio.get_running_loop().create_future()
        self._output.setDevice(_find_output(device))
        self.set_volume(volume)
        self._player.setSourceDevice(buffer, QUrl("tts.mp3"))  # la extensión indica el formato
        self._player.play()
        try:
            await asyncio.wait_for(self._done, MAX_PLAY_S)
        finally:  # también al saltar el mensaje (cancelación)
            self._done = None
            self._player.stop()
            self._player.setSourceDevice(None)
            buffer.close()
            buffer.deleteLater()

    def _on_status(self, status: QMediaPlayer.MediaStatus) -> None:
        if status in (QMediaPlayer.EndOfMedia, QMediaPlayer.InvalidMedia) and self._done and not self._done.done():
            self._done.set_result(None)

    def _on_error(self, _error, text: str) -> None:
        if self._done and not self._done.done():
            self._done.set_exception(RuntimeError(text or "error de audio"))
