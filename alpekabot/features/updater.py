"""Actualizaciones de la app desde GitHub Releases.

Cada versión publica el instalador, ``update.json`` y su firma ``update.json.sig``.
La firma Ed25519 cubre el manifiesto, y el manifiesto fija la versión y el SHA-256 del
instalador. Así, ni siquiera quien controle la cuenta de GitHub puede colar un
instalador falso sin la clave privada, que nunca sale del PC del desarrollador.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import subprocess
from base64 import b64decode
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .. import __version__
from ..paths import UPDATE_DIR
from ..update_key import UPDATE_PUBLIC_KEY

log = logging.getLogger(__name__)

REPO = "Loon-7w7/AlpekaBOT-App"
RELEASES_URL = f"https://github.com/{REPO}/releases"
RELEASES_API_URL = f"https://api.github.com/repos/{REPO}/releases?per_page=20"
MANIFEST_NAME = "update.json"
SIGNATURE_NAME = "update.json.sig"

MAX_MANIFEST_BYTES = 64 * 1024
MAX_API_BYTES = 1024 * 1024
SIGNATURE_BYTES = 64
MAX_INSTALLER_BYTES = 2 * 1024 ** 3
CHUNK_BYTES = 256 * 1024
CHECK_TIMEOUT_S = 20
READ_TIMEOUT_S = 60

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)\Z")
_BETA_TAG_RE = re.compile(r"^v(\d+\.\d+\.\d+)-beta\Z")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")

INSTALLER_ARGS = ("/SILENT", "/SP-", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS")


class UpdateError(Exception):
    """Fallo de la actualización, con un mensaje apto para el usuario."""


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    notes: str
    file: str
    size: int
    sha256: str
    tag: str = ""  # etiqueta de la release; vacío = «v<versión>» (las betas llevan «-beta»)

    @property
    def release_tag(self) -> str:
        return self.tag or f"v{self.version}"


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.match(text) if isinstance(text, str) else None
    return tuple(int(n) for n in match.groups()) if match else None  # type: ignore[return-value]


def verify_signature(data: bytes, signature: bytes, public_key: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(b64decode(public_key, validate=True)).verify(signature, data)
    except (InvalidSignature, ValueError):
        return False
    return True


def parse_manifest(raw: bytes, tag: str = "") -> UpdateInfo:
    """Valida el contenido de un manifiesto cuya firma ya se ha comprobado."""
    try:
        data = json.loads(raw)
        version, notes, file = data["version"], data.get("notes", ""), data["file"]
        size, sha256 = data["size"], data["sha256"]
    except (ValueError, KeyError, TypeError, AttributeError):
        raise UpdateError("El manifiesto de la actualización no es válido.") from None
    valid = (
        parse_version(version) is not None
        and isinstance(notes, str)
        and file == f"AlpekaBOT-Setup-{version}.exe"  # nombre fijo: nunca una ruta elegida por el servidor
        and type(size) is int and 0 < size <= MAX_INSTALLER_BYTES
        and isinstance(sha256, str) and _SHA256_RE.match(sha256) is not None
    )
    if not valid:
        raise UpdateError("El manifiesto de la actualización no es válido.")
    return UpdateInfo(version, notes, file, size, sha256, tag)


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _start_installer(path: Path) -> None:
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([str(path), *INSTALLER_ARGS], creationflags=flags, close_fds=True)  # noqa: S603


class Updater:
    def __init__(self, current: str = __version__, releases_url: str = RELEASES_URL,
                 public_key: str = UPDATE_PUBLIC_KEY, download_dir: Path = UPDATE_DIR,
                 api_url: str = RELEASES_API_URL, beta: bool = False) -> None:
        self.beta = beta  # también ofrece las pre-releases (canal beta)
        self._api_url = api_url
        self.current = current
        self._url = releases_url.rstrip("/")
        self._key = public_key
        self._dir = Path(download_dir)

    async def check(self) -> UpdateInfo | None:
        """Devuelve la versión publicada si es más nueva que la actual; si no, ``None``."""
        if not self._key:
            raise UpdateError("Esta compilación no tiene clave para verificar actualizaciones.")
        current = parse_version(self.current)
        if current is None:
            raise UpdateError(f"La versión actual ({self.current}) no tiene un formato válido.")
        timeout = aiohttp.ClientTimeout(total=CHECK_TIMEOUT_S)
        stable_error: UpdateError | None = None
        candidates: list[UpdateInfo] = []
        try:
            async with aiohttp.ClientSession(timeout=timeout) as http:
                try:
                    candidates.append(await self._manifest_at(http, f"{self._url}/latest/download"))
                except UpdateError as exc:
                    if not self.beta:
                        raise
                    stable_error = exc  # puede haber solo betas publicadas
                if self.beta:
                    beta = await self._latest_beta(http)
                    if beta is not None:
                        candidates.append(beta)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            raise UpdateError("No se pudo consultar si hay actualizaciones. Revisa tu conexión.") from None
        if not candidates:
            raise stable_error or UpdateError("No se pudo consultar si hay actualizaciones.")
        best = max(candidates, key=lambda i: parse_version(i.version))
        # Solo hacia delante: una versión antigua firmada no puede volver a instalarse
        return best if parse_version(best.version) > current else None

    async def _manifest_at(self, http: aiohttp.ClientSession, base: str, tag: str = "") -> UpdateInfo:
        raw = await self._fetch(http, f"{base}/{MANIFEST_NAME}", MAX_MANIFEST_BYTES)
        signature = await self._fetch(http, f"{base}/{SIGNATURE_NAME}", SIGNATURE_BYTES)
        if not verify_signature(raw, signature, self._key):
            log.warning("Se rechazó un manifiesto de actualización con la firma incorrecta")
            raise UpdateError("La actualización publicada no tiene una firma válida; se ha ignorado.")
        return parse_manifest(raw, tag)

    async def _latest_beta(self, http: aiohttp.ClientSession) -> UpdateInfo | None:
        """La beta más nueva publicada. Un fallo aquí no debe impedir ofrecer la versión estable."""
        try:
            releases = json.loads(await self._fetch(http, self._api_url, MAX_API_BYTES))
            tags = []
            for release in releases:
                match = _BETA_TAG_RE.match(release["tag_name"])
                if match and release.get("prerelease") and not release.get("draft"):
                    tags.append((parse_version(match.group(1)), release["tag_name"]))
            if not tags:
                return None
            version, tag = max(tags)
            info = await self._manifest_at(http, f"{self._url}/download/{tag}", tag)
            if parse_version(info.version) != version:  # el manifiesto firmado debe ser el de esa etiqueta
                raise UpdateError("El manifiesto de la beta no coincide con su etiqueta.")
            return info
        except (UpdateError, ValueError, KeyError, TypeError, AttributeError, aiohttp.ClientError) as exc:
            log.info("No se pudo comprobar la beta: %s", type(exc).__name__)
            return None

    async def _fetch(self, http: aiohttp.ClientSession, url: str, limit: int) -> bytes:
        async with http.get(url) as response:
            if response.status == 404:
                raise UpdateError("Todavía no hay ninguna versión publicada.")
            response.raise_for_status()
            data = b""
            while len(data) <= limit and (more := await response.content.read(limit + 1 - len(data))):
                data += more
        if len(data) > limit:
            raise UpdateError("El manifiesto de la actualización no es válido.")
        return data

    async def download(self, info: UpdateInfo, progress: Callable[[int, int], None] | None = None) -> Path:
        """Descarga el instalador, comprueba tamaño y SHA-256 y devuelve su ruta."""
        self._dir.mkdir(parents=True, exist_ok=True)
        target = self._dir / info.file
        part = target.with_name(target.name + ".part")
        digest = hashlib.sha256()
        received = 0
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=CHECK_TIMEOUT_S, sock_read=READ_TIMEOUT_S)
        url = f"{self._url}/download/{info.release_tag}/{info.file}"
        try:
            async with aiohttp.ClientSession(timeout=timeout) as http, http.get(url) as response:
                response.raise_for_status()
                with part.open("wb") as handle:
                    async for chunk in response.content.iter_chunked(CHUNK_BYTES):
                        received += len(chunk)
                        if received > info.size:
                            raise UpdateError("La descarga no coincide con la versión publicada.")
                        digest.update(chunk)
                        handle.write(chunk)
                        if progress is not None:
                            progress(received, info.size)
            if received != info.size or digest.hexdigest() != info.sha256:
                raise UpdateError("La descarga no coincide con la versión publicada.")
            part.replace(target)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            raise UpdateError("No se pudo descargar la actualización. Inténtalo de nuevo.") from None
        finally:
            part.unlink(missing_ok=True)  # también si se cancela
        return target

    async def launch(self, info: UpdateInfo, path: Path) -> None:
        """Vuelve a comprobar el instalador y lo ejecuta. La app debe cerrarse justo después."""
        loop = asyncio.get_running_loop()
        try:
            if await loop.run_in_executor(None, _sha256_of, path) != info.sha256:
                raise UpdateError("El instalador descargado ha cambiado; no se ejecutará.")
            # En un hilo aparte: las llamadas de Windows que lanzan procesos no deben frenar qasync
            await loop.run_in_executor(None, _start_installer, path)
        except OSError:
            raise UpdateError("No se pudo abrir el instalador.") from None

    def cleanup(self) -> None:
        """Borra los instaladores de actualizaciones anteriores."""
        if not self._dir.is_dir():
            return
        for path in self._dir.iterdir():
            try:
                if path.is_file():
                    path.unlink()
            except OSError:  # p. ej., el instalador aún se está cerrando
                log.debug("No se pudo borrar %s", path.name)
