"""Overlay «Sonando ahora» para OBS: la lógica sin red ni Qt (para poder testearla).

La página (``assets/overlay/now_playing.html``) recibe por WebSocket tres tipos de mensaje:

- ``config``: el estilo ya validado (variables CSS, interruptores, animación, CSS propio).
- ``state``: si se ve, si está en pausa y la canción (o ``None``).
- ``progress``: segundos actuales y duración.

Los textos (títulos de YouTube, nombres de viewers) los pinta la página con
``textContent``: nunca se interpretan como HTML.
"""

from __future__ import annotations

import re
from dataclasses import fields

from ..config import OverlayConfig
from .songs import SongStatus

ANIMATIONS = {"slide": "Deslizar", "fade": "Aparecer", "none": "Sin animación"}
LIMITS = {  # campo: (mínimo, máximo)
    "width": (240, 1200),
    "font_scale": (60, 200),
    "bg_opacity": (0, 100),
    "radius": (0, 40),
    "cover_size": (40, 200),
    "cover_radius": (0, 100),
    "port": (1024, 65535),
}
MAX_CSS = 20_000
MAX_HEADER = 40

_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_FONT_BAD = re.compile(r"[^\w \-.]")

# Lo que enseña la vista previa del editor cuando no suena nada
SAMPLE_SONG = {
    "id": "", "title": "Título de la canción de ejemplo", "channel": "Nombre del canal",
    "duration": 215, "requester": "Viewer", "platform": "twitch", "backup": False, "cover": "",
}
SAMPLE_PROGRESS = (83.0, 215.0)


def sanitize(cfg: OverlayConfig) -> None:
    """Corrige en el sitio los valores fuera de rango o con formato inválido (config.json editado a mano)."""
    d = OverlayConfig()
    for name, (lo, hi) in LIMITS.items():
        try:
            setattr(cfg, name, max(lo, min(hi, int(getattr(cfg, name)))))
        except (TypeError, ValueError):
            setattr(cfg, name, getattr(d, name))
    for name in ("accent", "background", "text_color", "subtext_color"):
        value = getattr(cfg, name)
        if not (isinstance(value, str) and _COLOR.fullmatch(value)):
            setattr(cfg, name, getattr(d, name))
    if cfg.animation not in ANIMATIONS:
        cfg.animation = d.animation
    cfg.font = _FONT_BAD.sub("", str(cfg.font)).strip()[:60] or d.font
    cfg.header_text = str(cfg.header_text)[:MAX_HEADER]
    cfg.custom_css = str(cfg.custom_css)[:MAX_CSS]
    for f in fields(OverlayConfig):
        if f.type in (bool, "bool"):
            setattr(cfg, f.name, bool(getattr(cfg, f.name)))


def rgba(color: str, alpha: float) -> str:
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return f"rgba({r}, {g}, {b}, {max(0.0, min(1.0, alpha)):.2f})"


def config_message(cfg: OverlayConfig) -> dict:
    sanitize(cfg)
    return {
        "type": "config",
        "vars": {
            "--w": f"{cfg.width}px",
            "--font": f'"{cfg.font}"',
            "--scale": f"{cfg.font_scale / 100:.2f}",
            "--accent": cfg.accent,
            "--accent-soft": rgba(cfg.accent, 0.18),
            "--border": rgba(cfg.accent, 0.45),
            "--bg": rgba(cfg.background, cfg.bg_opacity / 100),
            "--text": cfg.text_color,
            "--sub": cfg.subtext_color,
            "--radius": f"{cfg.radius}px",
            "--cover": f"{cfg.cover_size}px",
            "--cover-radius": f"{cfg.cover_radius}px",
        },
        "flags": {
            "border": cfg.border, "shadow": cfg.shadow, "blur": cfg.blur_cover,
            "show_header": cfg.show_header, "show_cover": cfg.show_cover, "show_channel": cfg.show_channel,
            "show_requester": cfg.show_requester, "show_platform": cfg.show_platform,
            "show_progress": cfg.show_progress, "show_time": cfg.show_time,
        },
        "animation": cfg.animation,
        "header_text": cfg.header_text,
        "custom_css": cfg.custom_css,
    }


def song_payload(st: SongStatus) -> dict | None:
    s = st.current
    if s is None:
        return None
    vid = s.video_id if _VIDEO_ID.fullmatch(s.video_id or "") else ""
    requester = "" if st.from_backup else (s.requested_by or "")
    return {
        "id": vid,
        "title": s.title,
        "channel": s.channel,
        "duration": int(s.duration_s or 0),
        "requester": requester,
        "platform": s.platform.value if requester and s.platform else None,
        "backup": bool(st.from_backup or not s.requested_by),
        "cover": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg" if vid else "",
    }


def state_message(st: SongStatus | None, cfg: OverlayConfig, preview: bool = False) -> dict:
    song = song_payload(st) if st is not None else None
    paused = bool(st.paused) if song else False
    if song is None:
        visible = not cfg.hide_when_idle
        if preview:
            song = dict(SAMPLE_SONG)
    else:
        visible = not (paused and cfg.hide_when_paused) and not (song["backup"] and not cfg.show_backup)
    if preview:
        visible = True  # en el editor siempre se ve, para poder diseñarlo
    return {"type": "state", "visible": visible, "paused": paused, "song": song}


def progress_message(pos: float, dur: float) -> dict:
    dur = max(0.0, float(dur or 0))
    pos = max(0.0, min(float(pos or 0), dur)) if dur else max(0.0, float(pos or 0))
    return {"type": "progress", "pos": round(pos, 2), "dur": round(dur, 2)}
