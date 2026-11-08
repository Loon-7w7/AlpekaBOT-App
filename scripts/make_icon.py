"""Genera alpekabot/assets/app.ico y app.png: el logo de la barra lateral, en grande."""

from __future__ import annotations

import os
import struct
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402

ASSETS = Path(__file__).resolve().parent.parent / "alpekabot" / "assets"
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def render(size: int) -> QImage:
    image = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, size, size), size * 0.24, size * 0.24)
    gradient = QLinearGradient(0, 0, 0, size)
    gradient.setColorAt(0, QColor("#a56bff"))
    gradient.setColorAt(1, QColor("#7a2fe6"))
    painter.fillPath(path, gradient)
    # En tamaños pequeños el robot ocupa más para que siga leyéndose
    share = 0.86 if size <= 24 else 0.72
    side = size * share
    offset = (size - side) / 2
    svg = (ASSETS / "icons" / "bot.svg").read_text(encoding="utf-8").replace("currentColor", "#ffffff")
    QSvgRenderer(QByteArray(svg.encode("utf-8"))).render(painter, QRectF(offset, offset, side, side))
    painter.end()
    return image


def png_bytes(image: QImage) -> bytes:
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def write_ico(path: Path, images: list[bytes]) -> None:
    """ICO con PNG dentro (Windows Vista en adelante), con todos los tamaños."""
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries = b""
    for size, data in zip(SIZES, images):
        side = 0 if size == 256 else size  # en ICO, 0 significa 256
        entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    path.write_bytes(header + entries + b"".join(images))


def main() -> None:
    app = QGuiApplication([])  # noqa: F841 (QSvgRenderer la necesita)
    images = [png_bytes(render(size)) for size in SIZES]
    write_ico(ASSETS / "app.ico", images)
    (ASSETS / "app.png").write_bytes(images[-1])


if __name__ == "__main__":
    main()
