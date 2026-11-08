"""Sustitución de variables en respuestas de comandos y mensajes de timers.

Variables disponibles:
    $user  $target  $args  $channel  $count  $platform
    $uptime  $game  $title
    $random(min,max)  $pick(a|b|c)  $readapi(url)

Seguridad:
- Se hace UNA sola pasada sobre la plantilla: lo que escribe un usuario
  ($target, $args) o lo que devuelve una URL nunca se interpreta como variable.
- $readapi solo accede a direcciones públicas de internet (no localhost ni la
  red local), con tiempo y tamaño limitados.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import random
import re
import socket
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from urllib.parse import urlparse

import aiohttp

from ..core.models import Platform, StreamInfo

log = logging.getLogger(__name__)

READAPI_TIMEOUT_S = 5
READAPI_MAX_BYTES = 4096
READAPI_MAX_CHARS = 400
READAPI_MAX_PER_MESSAGE = 3
RANDOM_MAX_SPAN = 1_000_000_000

_TOKEN_RE = re.compile(
    r"\$(?P<fn>readapi|random|pick)\((?P<arg>[^()]*)\)"
    r"|\$(?P<var>user|target|args|channel|count|platform|uptime|game|title|amount|reward|input)\b",
    re.IGNORECASE,
)

VARIABLES_HELP = (
    "$user · $target · $args · $channel · $count · $platform · "
    "$uptime · $game · $title · $random(1,100) · $pick(a|b|c) · $readapi(https://…)"
)

Fetcher = Callable[[str], Awaitable[str]]


@dataclass
class RenderContext:
    platform: Platform
    channel: str = ""
    user_name: str = ""
    args: list[str] = field(default_factory=list)
    count: int = 0
    stream: StreamInfo | None = None
    # Reacciones a eventos (fase 7): "amount", "reward", "input". Fuera de ellas quedan vacías.
    extra: dict[str, str] = field(default_factory=dict)


async def render(template: str, ctx: RenderContext, fetch: Fetcher | None = None) -> str:
    fetch = fetch or read_api
    out: list[str] = []
    pos = 0
    readapi_calls = 0
    for m in _TOKEN_RE.finditer(template):
        out.append(template[pos : m.start()])
        pos = m.end()
        fn, var = (m.group("fn") or "").lower(), (m.group("var") or "").lower()
        if fn == "readapi":
            readapi_calls += 1
            if readapi_calls > READAPI_MAX_PER_MESSAGE:
                out.append("[readapi: demasiadas llamadas]")
            else:
                out.append(await fetch(m.group("arg")))
        elif fn == "random":
            out.append(_random(m.group("arg"), m.group(0)))
        elif fn == "pick":
            out.append(_pick(m.group("arg"), m.group(0)))
        else:
            out.append(_variable(var, ctx))
    out.append(template[pos:])
    return "".join(out)


def _variable(name: str, ctx: RenderContext) -> str:
    stream = ctx.stream or StreamInfo(ctx.platform)
    if name == "user":
        return ctx.user_name
    if name == "target":
        return ctx.args[0].lstrip("@") if ctx.args else ctx.user_name
    if name == "args":
        return " ".join(ctx.args)
    if name == "channel":
        return ctx.channel
    if name == "count":
        return str(ctx.count)
    if name == "platform":
        return ctx.platform.label
    if name == "uptime":
        return stream.uptime_text
    if name == "game":
        return stream.game or "sin categoría"
    if name == "title":
        return stream.title or "sin título"
    return ctx.extra.get(name, "")


def _random(arg: str, original: str) -> str:
    try:
        a, b = (int(x.strip()) for x in arg.split(","))
    except ValueError:
        return original
    lo, hi = min(a, b), max(a, b)
    if hi - lo > RANDOM_MAX_SPAN:
        return original
    return str(random.randint(lo, hi))


def _pick(arg: str, original: str) -> str:
    options = [o.strip() for o in arg.split("|") if o.strip()]
    return random.choice(options) if options else original


# --------------------------------------------------------------- $readapi
async def _is_public_host(host: str, port: int) -> bool:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    addrs = {info[4][0] for info in infos}
    return bool(addrs) and all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addrs)


async def read_api(url: str) -> str:
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return "[readapi: URL no válida]"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not await _is_public_host(parsed.hostname, port):
        return "[readapi: dirección no permitida]"
    try:
        timeout = aiohttp.ClientTimeout(total=READAPI_TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Sin seguir redirecciones: podrían apuntar a la red local
            async with session.get(url, allow_redirects=False, headers={"User-Agent": "AlpekaBOT"}) as resp:
                if resp.status >= 300:
                    return f"[readapi: error {resp.status}]"
                data = await resp.content.read(READAPI_MAX_BYTES)
                charset = resp.charset or "utf-8"
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        log.info("$readapi falló para %s (%s)", parsed.hostname, type(exc).__name__)
        return "[readapi: sin respuesta]"
    text = " ".join(data.decode(charset, errors="replace").split())
    return text[:READAPI_MAX_CHARS]
