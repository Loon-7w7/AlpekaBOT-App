"""Actualizaciones: firma, manifiesto, descarga verificada y ejecución del instalador."""

from __future__ import annotations

import asyncio
import hashlib
import json
from base64 import b64encode

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from alpekabot.features import updater as up
from alpekabot.features.updater import UpdateError, UpdateInfo, Updater, parse_manifest, parse_version, verify_signature

INSTALLER = b"instalador falso " * 1000
VERSION = "1.0.1"
FILE = f"AlpekaBOT-Setup-{VERSION}.exe"


def _key() -> tuple[Ed25519PrivateKey, str]:
    private = Ed25519PrivateKey.generate()
    raw = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private, b64encode(raw).decode()


def _manifest(**over) -> bytes:
    data = {"version": VERSION, "notes": "Novedades", "file": FILE, "size": len(INSTALLER),
            "sha256": hashlib.sha256(INSTALLER).hexdigest()}
    data.update(over)
    return json.dumps(data).encode()


def _info(**over) -> UpdateInfo:
    return parse_manifest(_manifest(**over))


class _Server:
    """Imita las rutas de GitHub Releases, incluida la redirección de ``latest``."""

    def __init__(self, manifest: bytes, signature: bytes, installer: bytes = INSTALLER, stall: bool = False) -> None:
        self.manifest, self.signature, self.installer, self.stall = manifest, signature, installer, stall
        self.missing = False
        app = web.Application()
        app.router.add_get("/latest/download/{name}", self._latest)
        app.router.add_get(f"/download/v{VERSION}/{{name}}", self._asset)
        self.server = TestServer(app)

    async def _latest(self, request: web.Request) -> web.StreamResponse:
        if self.missing:
            raise web.HTTPNotFound()
        raise web.HTTPFound(f"/download/v{VERSION}/{request.match_info['name']}")

    async def _asset(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        if self.stall and name == FILE:  # envía un trozo y se queda colgado
            response = web.StreamResponse()
            response.content_length = len(self.installer)
            await response.prepare(request)
            await response.write(self.installer[:100])
            await asyncio.sleep(30)
            return response
        body = {up.MANIFEST_NAME: self.manifest, up.SIGNATURE_NAME: self.signature, FILE: self.installer}
        return web.Response(body=body[name])

    async def __aenter__(self) -> "_Server":
        await self.server.start_server()
        return self

    async def __aexit__(self, *_) -> None:
        await self.server.close()

    def updater(self, key: str, tmp_path, current: str = "1.0.0") -> Updater:
        return Updater(current, str(self.server.make_url("/")), key, tmp_path)


def _signed() -> tuple[bytes, bytes, str]:
    private, public = _key()
    manifest = _manifest()
    return manifest, private.sign(manifest), public


# ------------------------------------------------------------ versiones y firma
@pytest.mark.parametrize("text,expected", [("1.0.0", (1, 0, 0)), ("10.20.30", (10, 20, 30))])
def test_parse_version_valida(text, expected):
    assert parse_version(text) == expected


@pytest.mark.parametrize("text", ["1.0", "v1.0.0", "1.0.0-beta", "1.0.0\n", "", None, 100])
def test_parse_version_invalida(text):
    assert parse_version(text) is None


def test_compara_numeros_y_no_texto():
    assert parse_version("1.10.0") > parse_version("1.9.0")


def test_firma_correcta_y_manipulada():
    private, public = _key()
    assert verify_signature(b"datos", private.sign(b"datos"), public)
    assert not verify_signature(b"datos!", private.sign(b"datos"), public)


def test_firma_de_otra_clave_o_clave_rota():
    private, _ = _key()
    _, other = _key()
    signature = private.sign(b"datos")
    assert not verify_signature(b"datos", signature, other)
    assert not verify_signature(b"datos", signature, "esto no es base64!")
    assert not verify_signature(b"datos", b"corta", other)


# ------------------------------------------------------------------ manifiesto
def test_manifiesto_valido():
    info = parse_manifest(_manifest())
    assert (info.version, info.file, info.size) == (VERSION, FILE, len(INSTALLER))


@pytest.mark.parametrize("over", [
    {"file": "..\\..\\malo.exe"},
    {"file": "AlpekaBOT-Setup-9.9.9.exe"},
    {"version": "1.0"},
    {"size": 0},
    {"size": True},
    {"size": up.MAX_INSTALLER_BYTES + 1},
    {"sha256": "abc"},
    {"sha256": "A" * 64},
    {"notes": 5},
])
def test_manifiesto_invalido(over):
    with pytest.raises(UpdateError):
        parse_manifest(_manifest(**over))


@pytest.mark.parametrize("raw", [b"no es json", b"[]", b"{}", b"null"])
def test_manifiesto_con_formato_roto(raw):
    with pytest.raises(UpdateError):
        parse_manifest(raw)


# ---------------------------------------------------------------- comprobación
def test_detecta_version_nueva(tmp_path):
    manifest, signature, public = _signed()

    async def main():
        async with _Server(manifest, signature) as server:
            return await server.updater(public, tmp_path).check()

    assert asyncio.run(main()).version == VERSION


@pytest.mark.parametrize("current", [VERSION, "2.0.0"])
def test_no_ofrece_la_misma_ni_una_anterior(tmp_path, current):
    manifest, signature, public = _signed()

    async def main():
        async with _Server(manifest, signature) as server:
            return await server.updater(public, tmp_path, current).check()

    assert asyncio.run(main()) is None


def test_rechaza_manifiesto_alterado(tmp_path):
    _, signature, public = _signed()

    async def main():
        async with _Server(_manifest(notes="alterado"), signature) as server:
            await server.updater(public, tmp_path).check()

    with pytest.raises(UpdateError, match="firma"):
        asyncio.run(main())


def test_rechaza_manifiesto_enorme(tmp_path):
    _, signature, public = _signed()

    async def main():
        async with _Server(b"x" * (up.MAX_MANIFEST_BYTES + 10), signature) as server:
            await server.updater(public, tmp_path).check()

    with pytest.raises(UpdateError, match="no es válido"):
        asyncio.run(main())


def test_sin_versiones_publicadas(tmp_path):
    manifest, signature, public = _signed()

    async def main():
        async with _Server(manifest, signature) as server:
            server.missing = True
            await server.updater(public, tmp_path).check()

    with pytest.raises(UpdateError, match="Todavía no hay"):
        asyncio.run(main())


def test_sin_clave_no_consulta(tmp_path):
    with pytest.raises(UpdateError, match="clave"):
        asyncio.run(Updater("1.0.0", "http://127.0.0.1:9", "", tmp_path).check())


def test_sin_conexion_da_un_error_claro(tmp_path):
    _, public = _key()
    with pytest.raises(UpdateError, match="conexión"):
        asyncio.run(Updater("1.0.0", "http://127.0.0.1:9", public, tmp_path).check())


# -------------------------------------------------------------------- descarga
def test_descarga_correcta_con_progreso(tmp_path):
    manifest, signature, public = _signed()
    seen: list[tuple[int, int]] = []

    async def main():
        async with _Server(manifest, signature) as server:
            return await server.updater(public, tmp_path).download(_info(), lambda r, n: seen.append((r, n)))

    path = asyncio.run(main())
    assert path == tmp_path / FILE
    assert path.read_bytes() == INSTALLER
    assert seen[-1] == (len(INSTALLER), len(INSTALLER))
    assert [p.name for p in tmp_path.iterdir()] == [FILE]


@pytest.mark.parametrize("installer", [
    b"y" * len(INSTALLER),  # mismo tamaño, otro contenido
    INSTALLER + b"de mas",
    INSTALLER[:-1],
])
def test_descarga_que_no_coincide_no_deja_archivos(tmp_path, installer):
    manifest, signature, public = _signed()

    async def main():
        async with _Server(manifest, signature, installer) as server:
            await server.updater(public, tmp_path).download(_info())

    with pytest.raises(UpdateError, match="no coincide"):
        asyncio.run(main())
    assert list(tmp_path.iterdir()) == []


def test_cancelar_borra_la_descarga_a_medias(tmp_path):
    manifest, signature, public = _signed()

    async def main():
        started = asyncio.Event()
        async with _Server(manifest, signature, stall=True) as server:
            updater = server.updater(public, tmp_path)
            task = asyncio.ensure_future(updater.download(_info(), lambda *_: started.set()))
            await asyncio.wait_for(started.wait(), 5)
            assert (tmp_path / f"{FILE}.part").exists()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(main())
    assert list(tmp_path.iterdir()) == []


# -------------------------------------------------------- instalador y limpieza
def test_launch_ejecuta_el_instalador_en_silencio(tmp_path, monkeypatch):
    path = tmp_path / FILE
    path.write_bytes(INSTALLER)
    calls = []
    monkeypatch.setattr(up.subprocess, "Popen", lambda args, **kw: calls.append(args))
    asyncio.run(Updater("1.0.0", "http://x", "k", tmp_path).launch(_info(), path))
    assert calls == [[str(path), *up.INSTALLER_ARGS]]
    assert "/SILENT" in calls[0]


def test_launch_rechaza_un_instalador_modificado(tmp_path, monkeypatch):
    path = tmp_path / FILE
    path.write_bytes(b"modificado despues de descargar")
    calls = []
    monkeypatch.setattr(up.subprocess, "Popen", lambda args, **kw: calls.append(args))
    with pytest.raises(UpdateError, match="ha cambiado"):
        asyncio.run(Updater("1.0.0", "http://x", "k", tmp_path).launch(_info(), path))
    assert calls == []


def test_launch_sin_archivo_da_un_error_claro(tmp_path):
    with pytest.raises(UpdateError, match="abrir"):
        asyncio.run(Updater("1.0.0", "http://x", "k", tmp_path).launch(_info(), tmp_path / FILE))


def test_cleanup_borra_instaladores_antiguos(tmp_path):
    (tmp_path / "AlpekaBOT-Setup-0.9.0.exe").write_bytes(b"x")
    Updater("1.0.0", "http://x", "k", tmp_path).cleanup()
    assert list(tmp_path.iterdir()) == []


def test_cleanup_sin_carpeta(tmp_path):
    Updater("1.0.0", "http://x", "k", tmp_path / "no-existe").cleanup()


# ------------------------------------------------------------------------ rutas
def test_rutas_instalada_y_desde_codigo(monkeypatch, tmp_path):
    import importlib
    import sys

    from alpekabot import paths

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    try:
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        importlib.reload(paths)
        assert paths.DATA_DIR == tmp_path / "AlpekaBOT"
        assert paths.UPDATE_DIR == tmp_path / "AlpekaBOT" / "updates"
    finally:
        monkeypatch.delattr(sys, "frozen", raising=False)
        importlib.reload(paths)
    assert paths.DATA_DIR.name == "data"
    assert not paths.IS_FROZEN
