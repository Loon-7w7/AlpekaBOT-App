"""Genera y publica versiones de AlpekaBOT.

    python scripts/release.py keygen             crea la clave de firma (una sola vez)
    python scripts/release.py export-key RUTA    guarda una copia de la clave privada
    python scripts/release.py build NOTAS.txt    empaqueta, crea el instalador y lo firma
    python scripts/release.py publish            sube dist/release a GitHub Releases

La clave privada vive en el Administrador de credenciales de Windows y nunca se imprime.
Si se pierde, las copias ya instaladas no aceptarán más actualizaciones.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from base64 import b64decode, b64encode
from importlib import metadata
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from alpekabot import __version__, credentials  # noqa: E402
from alpekabot.features.updater import MANIFEST_NAME, REPO, SIGNATURE_NAME, parse_version  # noqa: E402
from alpekabot.update_key import UPDATE_PUBLIC_KEY  # noqa: E402

SIGNING_KEY = "release/signing_key"
KEY_MODULE = ROOT / "alpekabot" / "update_key.py"
RELEASE_DIR = ROOT / "dist" / "release"
ISCC_CANDIDATES = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
    Path(os.environ.get("ProgramFiles(x86)", "")) / "Inno Setup 6" / "ISCC.exe",
    Path(os.environ.get("ProgramFiles", "")) / "Inno Setup 6" / "ISCC.exe",
]


class ReleaseError(Exception):
    pass


def public_key_of(private: Ed25519PrivateKey) -> str:
    return b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()


def load_signing_key(expected_public: str) -> Ed25519PrivateKey:
    secret = credentials.get_secret(SIGNING_KEY)
    if not secret:
        raise ReleaseError("No hay clave de firma en este PC. Usa keygen o restaura tu copia.")
    private = Ed25519PrivateKey.from_private_bytes(b64decode(secret))
    if public_key_of(private) != expected_public:
        raise ReleaseError("La clave de firma no coincide con la clave pública de la app.")
    return private


def write_release_files(installer: Path, version: str, notes: str, private: Ed25519PrivateKey) -> bytes:
    """Escribe el manifiesto y su firma junto al instalador."""
    digest = hashlib.sha256()
    with installer.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    manifest = json.dumps(
        {"version": version, "notes": notes.strip(), "file": installer.name,
         "size": installer.stat().st_size, "sha256": digest.hexdigest()},
        ensure_ascii=False, indent=2,
    ).encode("utf-8")
    (installer.parent / MANIFEST_NAME).write_bytes(manifest)
    (installer.parent / SIGNATURE_NAME).write_bytes(private.sign(manifest))
    return manifest


def _runtime_dists() -> list[metadata.Distribution]:
    """Las dependencias de requirements.txt y las suyas, sin las de desarrollo."""
    name_re = re.compile(r"^[A-Za-z0-9._-]+")
    pending = [m.group(0) for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
               if (m := name_re.match(line.strip()))]
    found: dict[str, metadata.Distribution] = {}
    while pending:
        name = pending.pop().lower().replace("_", "-")
        if name in found:
            continue
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:  # solo para otro sistema operativo
            continue
        found[name] = dist
        for requirement in dist.requires or []:
            if "extra ==" not in requirement and (m := name_re.match(requirement)):
                pending.append(m.group(0))
    return [found[name] for name in sorted(found)]


def _license_of(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    if meta.get("License-Expression"):
        return meta["License-Expression"]
    classifiers = [c.rsplit(" :: ", 1)[-1] for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    if classifiers:
        return ", ".join(classifiers)
    text = meta.get("License") or ""
    return text.splitlines()[0][:100] if text.strip() else "Consulta el proyecto original"


def write_notices(target: Path) -> None:
    """Escribe los avisos de licencia que deben acompañar a la app distribuida."""
    parts = ["AlpekaBOT incluye el siguiente software de terceros.\n\n"
             "Qt y PySide6 se distribuyen bajo LGPLv3: sus bibliotecas están en la carpeta _internal y se "
             "pueden sustituir por versiones compatibles. Python usa la licencia PSF. Las fuentes y los "
             "iconos llevan su licencia junto a ellos, en _internal/alpekabot/assets.\n"]
    for dist in _runtime_dists():
        parts.append(f"\n{'=' * 72}\n{dist.name} {dist.version}\nLicencia: {_license_of(dist)}\n")
        for file in dist.files or []:
            if re.search(r"(licen[cs]e|copying|notice)", file.name, re.I) and "dist-info" in str(file):
                try:
                    text = Path(file.locate()).read_text(encoding="utf-8", errors="replace")
                    parts.append(f"\n--- {file.name} ---\n{text}\n")
                except OSError:
                    pass
    target.write_text("".join(parts), encoding="utf-8")


def cmd_keygen(_: argparse.Namespace) -> None:
    if UPDATE_PUBLIC_KEY or credentials.get_secret(SIGNING_KEY):
        raise ReleaseError("Ya existe una clave de firma. No se reemplaza: dejaría sin actualizaciones a quien ya instaló.")
    private = Ed25519PrivateKey.generate()
    raw = private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    credentials.set_secret(SIGNING_KEY, b64encode(raw).decode())
    text = KEY_MODULE.read_text(encoding="utf-8").replace(
        'UPDATE_PUBLIC_KEY = ""', f'UPDATE_PUBLIC_KEY = "{public_key_of(private)}"')
    KEY_MODULE.write_text(text, encoding="utf-8")
    print("Clave creada. Guarda una copia con: python scripts/release.py export-key RUTA")


def cmd_export_key(args: argparse.Namespace) -> None:
    secret = credentials.get_secret(SIGNING_KEY)
    if not secret:
        raise ReleaseError("No hay clave de firma en este PC.")
    target = Path(args.path)
    if target.exists():
        raise ReleaseError(f"{target} ya existe; no se sobrescribe.")
    target.write_text(secret, encoding="utf-8")
    print(f"Copia guardada en {target}. Protégela y no la subas a ningún repositorio.")


def _find_iscc() -> Path:
    found = shutil.which("ISCC")
    if found:
        return Path(found)
    for path in ISCC_CANDIDATES:
        if path.is_file():
            return path
    raise ReleaseError("No se encontró Inno Setup 6 (ISCC.exe).")


def cmd_build(args: argparse.Namespace) -> None:
    if parse_version(__version__) is None:
        raise ReleaseError(f"Versión inválida: {__version__}")
    notes = Path(args.notes).read_text(encoding="utf-8")
    private = load_signing_key(UPDATE_PUBLIC_KEY)  # falla antes de los minutos de compilación
    iscc = _find_iscc()
    shutil.rmtree(RELEASE_DIR, ignore_errors=True)
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "alpekabot.spec"], cwd=ROOT, check=True)
    write_notices(ROOT / "dist" / "AlpekaBOT" / "THIRD_PARTY_NOTICES.txt")
    subprocess.run([str(iscc), "/Qp", f"/DAppVersion={__version__}", "installer/AlpekaBOT.iss"], cwd=ROOT, check=True)
    installer = RELEASE_DIR / f"AlpekaBOT-Setup-{__version__}.exe"
    write_release_files(installer, __version__, notes, private)
    print(f"Listo: {installer} ({installer.stat().st_size / 1024 ** 2:.0f} MB)")


def cmd_publish(_: argparse.Namespace) -> None:
    installer = RELEASE_DIR / f"AlpekaBOT-Setup-{__version__}.exe"
    files = [installer, RELEASE_DIR / MANIFEST_NAME, RELEASE_DIR / SIGNATURE_NAME]
    missing = [f.name for f in files if not f.is_file()]
    if missing:
        raise ReleaseError("Faltan archivos (ejecuta build primero): " + ", ".join(missing))
    notes = json.loads(files[1].read_text(encoding="utf-8"))["notes"]
    subprocess.run(["gh", "release", "create", f"v{__version__}", *map(str, files), "--repo", REPO,
                    "--title", f"AlpekaBOT {__version__}", "--notes", notes], check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(required=True)
    commands.add_parser("keygen").set_defaults(run=cmd_keygen)
    export = commands.add_parser("export-key")
    export.add_argument("path")
    export.set_defaults(run=cmd_export_key)
    build = commands.add_parser("build")
    build.add_argument("notes", help="archivo de texto con las novedades")
    build.set_defaults(run=cmd_build)
    commands.add_parser("publish").set_defaults(run=cmd_publish)
    args = parser.parse_args()
    try:
        args.run(args)
    except (ReleaseError, subprocess.CalledProcessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
