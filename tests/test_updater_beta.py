"""Canal beta del actualizador: pre-releases de GitHub, siempre firmadas y nunca hacia atrás."""

from __future__ import annotations

import asyncio
import hashlib
import json
from base64 import b64encode

from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from alpekabot.features import updater as up
from alpekabot.features.updater import Updater

INSTALLER = b"instalador beta " * 500


def _manifest(version: str) -> bytes:
    return json.dumps({"version": version, "notes": "n", "file": f"AlpekaBOT-Setup-{version}.exe",
                       "size": len(INSTALLER), "sha256": hashlib.sha256(INSTALLER).hexdigest()}).encode()


class _Github:
    """Estable 1.3.2 en `latest` y, opcionalmente, una beta publicada como pre-release."""

    def __init__(self, private: Ed25519PrivateKey, beta_version: str | None = "1.4.0", api_ok: bool = True) -> None:
        self.private, self.beta, self.api_ok = private, beta_version, api_ok
        app = web.Application()
        app.router.add_get("/api", self._api)
        app.router.add_get("/latest/download/{name}", self._latest)
        app.router.add_get("/download/{tag}/{name}", self._asset)
        self.server = TestServer(app)

    async def _api(self, _: web.Request) -> web.Response:
        if not self.api_ok:
            raise web.HTTPForbidden()
        releases = [{"tag_name": "v1.3.2", "prerelease": False, "draft": False}]
        if self.beta:
            releases.append({"tag_name": f"v{self.beta}-beta", "prerelease": True, "draft": False})
            releases.append({"tag_name": "v9.9.9-beta", "prerelease": True, "draft": True})  # borrador: se ignora
        return web.json_response(releases)

    async def _latest(self, request: web.Request) -> web.StreamResponse:
        raise web.HTTPFound(f"/download/v1.3.2/{request.match_info['name']}")

    async def _asset(self, request: web.Request) -> web.Response:
        tag, name = request.match_info["tag"], request.match_info["name"]
        version = tag.removeprefix("v").removesuffix("-beta")
        manifest = _manifest(version)
        body = {up.MANIFEST_NAME: manifest, up.SIGNATURE_NAME: self.private.sign(manifest),
                f"AlpekaBOT-Setup-{version}.exe": INSTALLER}
        if name not in body:
            raise web.HTTPNotFound()
        return web.Response(body=body[name])

    async def __aenter__(self) -> "_Github":
        await self.server.start_server()
        return self

    async def __aexit__(self, *_) -> None:
        await self.server.close()

    def updater(self, tmp_path, current: str, beta: bool) -> Updater:
        public = b64encode(self.private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
        return Updater(current, str(self.server.make_url("/")), public, tmp_path,
                       api_url=str(self.server.make_url("/api")), beta=beta)


def _check(tmp_path, current="1.3.1", beta=True, **kwargs):
    async def main():
        async with _Github(Ed25519PrivateKey.generate(), **kwargs) as gh:
            return await gh.updater(tmp_path, current, beta).check()

    return asyncio.run(main())


def test_con_beta_ofrece_la_pre_release_mas_nueva(tmp_path):
    info = _check(tmp_path)
    assert (info.version, info.release_tag) == ("1.4.0", "v1.4.0-beta")


def test_sin_beta_ofrece_solo_la_estable(tmp_path):
    info = _check(tmp_path, beta=False)
    assert (info.version, info.release_tag) == ("1.3.2", "v1.3.2")


def test_si_la_estable_supera_a_la_beta_gana_la_estable(tmp_path):
    assert _check(tmp_path, beta_version="1.3.0").version == "1.3.2"


def test_fallo_de_la_api_no_impide_ofrecer_la_estable(tmp_path):
    assert _check(tmp_path, api_ok=False).version == "1.3.2"


def test_no_baja_de_version_al_estar_en_una_beta_mas_nueva(tmp_path):
    assert _check(tmp_path, current="1.4.0", beta=False) is None


def test_descarga_la_beta_desde_su_etiqueta(tmp_path):
    async def main():
        async with _Github(Ed25519PrivateKey.generate()) as gh:
            updater = gh.updater(tmp_path, "1.3.1", True)
            return await updater.download(await updater.check())

    assert asyncio.run(main()).read_bytes() == INSTALLER
