"""Cliente mínimo de la API oficial YouTube Data v3 (búsqueda y datos de vídeos).

La clave de API va en la URL de cada petición, así que NUNCA se muestra ni se
registra el texto de las excepciones de red (contienen la URL con la clave).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Callable
from urllib.parse import parse_qs, urlparse

import aiohttp

log = logging.getLogger(__name__)

API_BASE = "https://www.googleapis.com/youtube/v3/"
API_KEY_SECRET = "youtube/api_key"  # clave en el Administrador de credenciales (credentials.py)
TIMEOUT_S = 8

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_DURATION_RE = re.compile(r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")
_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}


class YouTubeError(Exception):
    """Error con un mensaje apto para mostrar en el chat o en la app."""


@dataclass(frozen=True)
class VideoInfo:
    video_id: str
    title: str
    channel: str
    channel_id: str
    duration_s: int
    embeddable: bool = True
    is_live: bool = False


def parse_video_id(text: str) -> str | None:
    """Extrae el ID de un enlace de YouTube. Devuelve None si no es un enlace."""
    text = text.strip()
    if not text.startswith(("http://", "https://")):
        text = "https://" + text if text.split("/")[0].lower() in _YT_HOSTS else text
    try:
        u = urlparse(text)
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    if host not in _YT_HOSTS:
        return None
    candidate = None
    if host.endswith("youtu.be"):
        candidate = u.path.lstrip("/").split("/")[0]
    elif u.path == "/watch":
        candidate = parse_qs(u.query).get("v", [None])[0]
    else:
        parts = [p for p in u.path.split("/") if p]
        if len(parts) >= 2 and parts[0] in ("shorts", "embed", "live", "v"):
            candidate = parts[1]
    return candidate if candidate and _ID_RE.match(candidate) else None


def parse_duration(iso: str) -> int:
    """'PT1H2M3S' -> 3723 segundos. Desconocida -> 0."""
    m = _DURATION_RE.match(iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


def format_duration(seconds: int) -> str:
    h, rem = divmod(max(0, seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class YouTubeClient:
    def __init__(self, key_getter: Callable[[], str]) -> None:
        self._key_getter = key_getter

    async def _get(self, endpoint: str, params: dict) -> dict:
        key = self._key_getter()
        if not key:
            raise YouTubeError("Falta la clave de API de YouTube (Configuración → YouTube).")
        try:
            timeout = aiohttp.ClientTimeout(total=TIMEOUT_S)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(API_BASE + endpoint, params={**params, "key": key}) as resp:
                    data = await resp.json(content_type=None)
                    status = resp.status
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            log.warning("YouTube API: fallo de red (%s)", type(exc).__name__)
            raise YouTubeError("No se pudo contactar con YouTube.") from None
        if status != 200:
            reasons = {e.get("reason") for e in (data.get("error", {}).get("errors") or [])}
            log.warning("YouTube API: HTTP %s %s", status, sorted(r for r in reasons if r))
            if "quotaExceeded" in reasons or "dailyLimitExceeded" in reasons:
                raise YouTubeError("Se agotó la cuota diaria de la API de YouTube. Usa un enlace o prueba mañana.")
            if status in (400, 403) and reasons & {"keyInvalid", "badRequest", "accessNotConfigured", "forbidden"}:
                raise YouTubeError("La clave de API de YouTube no es válida o la API no está activada.")
            raise YouTubeError("YouTube devolvió un error.")
        return data

    async def get_videos(self, ids: list[str]) -> list[VideoInfo]:
        if not ids:
            return []
        data = await self._get("videos", {"part": "snippet,contentDetails,status", "id": ",".join(ids)})
        out = []
        for it in data.get("items", []):
            sn, cd, st = it.get("snippet", {}), it.get("contentDetails", {}), it.get("status", {})
            out.append(VideoInfo(
                video_id=it["id"],
                title=sn.get("title", ""),
                channel=sn.get("channelTitle", ""),
                channel_id=sn.get("channelId", ""),
                duration_s=parse_duration(cd.get("duration", "")),
                embeddable=bool(st.get("embeddable", True)),
                is_live=sn.get("liveBroadcastContent", "none") != "none",
            ))
        order = {vid: i for i, vid in enumerate(ids)}
        return sorted(out, key=lambda v: order.get(v.video_id, 99))

    async def get_video(self, video_id: str) -> VideoInfo | None:
        vids = await self.get_videos([video_id])
        return vids[0] if vids else None

    async def search(self, query: str) -> VideoInfo | None:
        """Primer resultado que no sea un directo. Los no incrustables también valen
        (el reproductor los abre en la página de YouTube)."""
        data = await self._get("search", {
            "part": "snippet", "type": "video",
            "maxResults": 5, "q": query[:200], "safeSearch": "moderate",
        })
        ids = [it["id"]["videoId"] for it in data.get("items", []) if it.get("id", {}).get("videoId")]
        for v in await self.get_videos(ids):
            if not v.is_live:
                return v
        return None
