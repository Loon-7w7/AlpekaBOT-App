"""Ventana de actualización y archivos de una versión."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import sys
from base64 import b64encode
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from alpekabot.features.updater import UpdateError, UpdateInfo, parse_manifest, verify_signature
from alpekabot.ui.update_dialog import UpdateDialog

INFO = UpdateInfo("1.0.1", "<b>Novedades</b>", "AlpekaBOT-Setup-1.0.1.exe", 10, "a" * 64)


class _Updater:
    current = "1.0.0"

    def __init__(self, result=None, hang: bool = False) -> None:
        self.result, self.hang = result, hang
        self.cancelled = False

    async def download(self, info, progress):
        progress(5, 10)
        if self.hang:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _flush():
    return asyncio.sleep(0.01)


def test_descarga_correcta_pide_instalar():
    async def main():
        dialog = UpdateDialog(_Updater(Path("instalador.exe")), INFO, False)
        seen = []
        dialog.install_requested.connect(lambda info, path: seen.append((info, path)))
        dialog._update.click()
        await _flush()
        return seen

    assert asyncio.run(main()) == [(INFO, Path("instalador.exe"))]


def test_error_se_muestra_y_permite_reintentar():
    async def main():
        dialog = UpdateDialog(_Updater(UpdateError("La descarga falló")), INFO, False)
        seen = []
        dialog.install_requested.connect(lambda *a: seen.append(a))
        dialog._update.click()
        await _flush()
        assert not dialog._error.isHidden()
        assert dialog._error.text() == "La descarga falló"
        assert dialog._update.isEnabled()
        assert dialog._later.text() == "Más tarde"
        assert seen == []

    asyncio.run(main())


def test_cancelar_detiene_la_descarga():
    async def main():
        updater = _Updater(hang=True)
        dialog = UpdateDialog(updater, INFO, False)
        dialog._update.click()
        await _flush()
        assert dialog._later.text() == "Cancelar"
        dialog._later.click()
        await _flush()
        assert updater.cancelled

    asyncio.run(main())


def test_avisa_si_hay_conexion_y_no_interpreta_html():
    from PySide6.QtWidgets import QLabel, QPlainTextEdit

    async def main():
        connected = UpdateDialog(_Updater(), INFO, True)
        idle = UpdateDialog(_Updater(), INFO, False)
        text = lambda d: " ".join(w.text() for w in d.findChildren(QLabel))  # noqa: E731
        assert "desconectará" in text(connected)
        assert "desconectará" not in text(idle)
        assert connected.findChild(QPlainTextEdit).toPlainText() == "<b>Novedades</b>"

    asyncio.run(main())


@pytest.fixture
def release():
    path = Path(__file__).resolve().parent.parent / "scripts" / "release.py"
    spec = importlib.util.spec_from_file_location("alpekabot_release", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    del sys.modules[spec.name]


def test_los_archivos_firmados_los_acepta_la_app(release, tmp_path):
    private = Ed25519PrivateKey.generate()
    installer = tmp_path / "AlpekaBOT-Setup-1.0.1.exe"
    installer.write_bytes(b"instalador")
    release.write_release_files(installer, "1.0.1", "  Ñandú y más  \n", private)
    raw = (tmp_path / "update.json").read_bytes()
    assert verify_signature(raw, (tmp_path / "update.json.sig").read_bytes(), release.public_key_of(private))
    info = parse_manifest(raw)
    assert info.notes == "Ñandú y más"
    assert info.size == 10
    assert info.sha256 == hashlib.sha256(b"instalador").hexdigest()
    assert json.loads(raw)["file"] == installer.name


def test_rechaza_una_clave_que_no_coincide_con_la_app(release, monkeypatch):
    private = Ed25519PrivateKey.generate()
    raw = private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    monkeypatch.setattr(release.credentials, "get_secret", lambda _: b64encode(raw).decode())
    assert release.load_signing_key(release.public_key_of(private)) is not None
    with pytest.raises(release.ReleaseError, match="no coincide"):
        release.load_signing_key(release.public_key_of(Ed25519PrivateKey.generate()))


def test_avisos_incluyen_dependencias_y_no_las_de_desarrollo(release, tmp_path):
    target = tmp_path / "avisos.txt"
    release.write_notices(target)
    text = target.read_text(encoding="utf-8")
    for name in ("PySide6", "aiohttp", "cryptography", "edge_tts"):
        assert name.lower().replace("_", "-") in text.lower().replace("_", "-")
    assert "\npytest " not in text
    assert "\npyinstaller " not in text.lower()


def test_iconos_del_actualizador_existen():
    from alpekabot.ui import theme

    for name in ("download", "refresh-cw"):
        assert (theme.ASSETS / "icons" / f"{name}.svg").is_file()
