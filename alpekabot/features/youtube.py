"""Datos de vídeos de YouTube (búsqueda y vídeo por ID) para las peticiones de canciones.

Dos fuentes:

* **InnerTube** (por defecto, sin clave): la API interna que usa la propia web de
  youtube.com (``/youtubei/v1/search`` y ``/youtubei/v1/player``). No es oficial y
  YouTube puede cambiarla, pero no necesita clave ni tiene cuota compartida: cada
  usuario consulta desde su propia conexión.
* **API oficial YouTube Data v3** (opcional): si el usuario pone su clave. Si falla
  (cuota agotada, clave mala, red), se pasa a InnerTube y la oficial descansa un rato.

La clave de API va en la URL de cada petición, así que NUNCA se muestra ni se
registra el texto de las excepciones de red (contienen la URL con la clave).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import parse_qs, urlparse

import aiohttp

log = logging.getLogger(__name__)

API_BASE = "https://www.googleapis.com/youtube/v3/"
API_KEY_SECRET = "youtube/api_key"  # clave en el Administrador de credenciales (credentials.py)
TIMEOUT_S = 8

INNERTUBE_BASE = "https://www.youtube.com/youtubei/v1/"
_INNERTUBE_VERSION = "2.20250101.00.00"
_INNERTUBE_CONTEXT = {"client": {"clientName": "WEB", "clientVersion": _INNERTUBE_VERSION, "hl": "es"}}
_INNERTUBE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "X-YouTube-Client-Name": "1",
    "X-YouTube-Client-Version": _INNERTUBE_VERSION,
}
_ONLY_VIDEOS = "EgIQAQ%3D%3D"  # filtro de la búsqueda: solo vídeos (sin canales ni listas)
OFFICIAL_PAUSE_S = 15 * 60  # tras un fallo de la API oficial, se usa InnerTube durante este tiempo

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


def parse_clock(text: str) -> int:
    """'6:10:58' -> 22258 segundos. Desconocida -> 0."""
    try:
        parts = [int(p) for p in (text or "").strip().split(":")]
    except ValueError:
        return 0
    if not parts or len(parts) > 3:
        return 0
    total = 0
    for p in parts:
        total = total * 60 + p
    return total


def _text(node: dict | None) -> str:
    """Texto de un nodo de InnerTube: ``{"simpleText": ...}`` o ``{"runs": [{"text": ...}]}``."""
    if not node:
        return ""
    if "simpleText" in node:
        return str(node["simpleText"])
    return "".join(str(r.get("text", "")) for r in node.get("runs") or [])


def _find_video_renderers(node) -> list[dict]:
    """Todos los ``videoRenderer`` de la respuesta, en orden. Se recorre el árbol entero
    para no depender de dónde los coloque YouTube."""
    found: list[dict] = []
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            vr = cur.get("videoRenderer")
            if isinstance(vr, dict) and vr.get("videoId"):
                found.append(vr)
            stack.extend(reversed([v for k, v in cur.items() if k != "videoRenderer"]))
        elif isinstance(cur, list):
            stack.extend(reversed(cur))
    return found


class InnerTube:
    """Datos de YouTube sin clave, con la API interna de la web (no oficial)."""

    async def _post(self, endpoint: str, body: dict) -> dict:
        try:
            timeout = aiohttp.ClientTimeout(total=TIMEOUT_S)
            async with aiohttp.ClientSession(timeout=timeout, headers=_INNERTUBE_HEADERS) as session:
                async with session.post(INNERTUBE_BASE + endpoint + "?prettyPrint=false",
                                        json={"context": _INNERTUBE_CONTEXT, **body}) as resp:
                    status = resp.status
                    data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            log.warning("YouTube (sin clave): fallo de red (%s)", type(exc).__name__)
            raise YouTubeError("No se pudo contactar con YouTube.") from None
        if status != 200 or not isinstance(data, dict):
            log.warning("YouTube (sin clave): HTTP %s en %s", status, endpoint)
            raise YouTubeError("YouTube devolvió un error.")
        return data

    async def get_video(self, video_id: str) -> VideoInfo | None:
        data = await self._post("player", {"videoId": video_id})
        vd = data.get("videoDetails") or {}
        if vd.get("videoId") != video_id:
            return None  # no existe, es privado o lo han borrado
        ps = data.get("playabilityStatus") or {}
        try:
            duration = int(vd.get("lengthSeconds") or 0)
        except (TypeError, ValueError):
            duration = 0
        return VideoInfo(
            video_id=video_id,
            title=str(vd.get("title") or ""),
            channel=str(vd.get("author") or ""),
            channel_id=str(vd.get("channelId") or ""),
            duration_s=duration,
            embeddable=ps.get("playableInEmbed") is not False,
            is_live=bool(vd.get("isLive") or vd.get("isUpcoming")),
        )

    async def search(self, query: str) -> VideoInfo | None:
        """Primer resultado que no sea un directo (los directos no traen duración)."""
        data = await self._post("search", {"query": query[:200], "params": _ONLY_VIDEOS})
        renderers = _find_video_renderers(data)
        if not renderers:
            log.info("YouTube (sin clave): la búsqueda no devolvió vídeos")
        for vr in renderers:
            badges = {(b.get("metadataBadgeRenderer") or {}).get("style") for b in vr.get("badges") or []}
            duration = parse_clock(_text(vr.get("lengthText")))
            if not duration or "BADGE_STYLE_TYPE_LIVE_NOW" in badges or vr.get("upcomingEventData"):
                continue
            # El canal de la búsqueda puede ser el del artista («Luis Fonsi») y no el que
            # sube el vídeo («LuisFonsiVEVO»); los datos por ID coinciden con los de un
            # enlace y con la API oficial, así la lista negra por canal no depende de cómo se pidió.
            try:
                full = await self.get_video(str(vr["videoId"]))
            except YouTubeError:
                full = None
            if full and not full.is_live:
                return full
            owner = ((vr.get("ownerText") or vr.get("longBylineText") or {}).get("runs") or [{}])[0]
            return VideoInfo(
                video_id=str(vr["videoId"]),
                title=_text(vr.get("title")),
                channel=str(owner.get("text") or ""),
                channel_id=str(((owner.get("navigationEndpoint") or {}).get("browseEndpoint") or {})
                               .get("browseId") or ""),
                duration_s=duration,
            )
        return None


class YouTubeClient:
    """Lo que usa ``SongService``: API oficial si hay clave (y funciona); si no, InnerTube."""

    def __init__(self, key_getter: Callable[[], str], clock: Callable[[], float] = time.monotonic) -> None:
        self._key_getter = key_getter
        self._clock = clock
        self.official = OfficialAPI(key_getter)
        self.innertube = InnerTube()
        self._official_paused_until = 0.0

    async def get_video(self, video_id: str) -> VideoInfo | None:
        return await self._call("get_video", video_id)

    async def search(self, query: str) -> VideoInfo | None:
        return await self._call("search", query)

    async def _call(self, method: str, arg: str) -> VideoInfo | None:
        if self._key_getter() and self._clock() >= self._official_paused_until:
            try:
                return await getattr(self.official, method)(arg)
            except YouTubeError as exc:  # mensajes propios: no llevan la clave
                self._official_paused_until = self._clock() + OFFICIAL_PAUSE_S
                log.warning("API oficial de YouTube: %s Se usa el modo sin clave durante %d min.",
                            exc, OFFICIAL_PAUSE_S // 60)
        return await getattr(self.innertube, method)(arg)


class OfficialAPI:
    """API oficial YouTube Data v3 con la clave del usuario."""

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
