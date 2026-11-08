"""Reproductor de YouTube dentro de la app, con dos modos.

1. **Incrustado** (por defecto): API oficial IFrame en un QWebEngineView. YouTube no
   permite reproducir desde páginas file://, así que la página se sirve con un mini
   servidor aiohttp en 127.0.0.1 (puerto aleatorio). La página avisa de sus eventos
   con POST /event. Un token aleatorio por sesión impide que otra web abierta en tu
   navegador controle el reproductor.

2. **Página de YouTube** (respaldo): muchos vídeos musicales dan el error 101/150/153
   («el autor no permite reproducirlo fuera de YouTube») en el reproductor incrustado,
   pero sí se pueden ver en youtube.com. Cuando pasa, se abre la página normal
   ``youtube.com/watch`` en otra vista y se controla su ``<video>`` con un script que
   corre en un *mundo aislado* (la página de YouTube no puede ver ni tocar sus
   variables). El script avisa por ``console.log`` con un prefijo secreto.

Las dos vistas llevan el bloqueo de anuncios de ``adblock.py``.

Implementa el protocolo ``features.songs.Player``.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from typing import Callable

from aiohttp import web
from PySide6.QtCore import QObject, QUrl
from PySide6.QtGui import QColor
from PySide6.QtWebEngineCore import (
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineScript,
    QWebEngineSettings,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QStackedWidget

from ..paths import DATA_DIR
from . import adblock

log = logging.getLogger(__name__)

# Errores del reproductor incrustado que se reintentan en la página de YouTube
EMBED_BLOCKED_CODES = {101, 150, 152, 153}
# Error propio: tampoco se pudo en la página de YouTube (edad, región, inicio de sesión...)
WATCH_FAILED_CODE = 1000

_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<style>html,body{margin:0;height:100%;background:#000;overflow:hidden}#p{width:100%;height:100%}</style>
</head><body><div id="p"></div>
<script>
const TOKEN = __TOKEN__;
let current = "";  // vídeo que se pidió cargar: cada evento lo incluye para descartar eventos atrasados
function loadedId(){ try { return (player && player.getVideoData && player.getVideoData().video_id) || current; } catch(e) { return current; } }
function send(o){ o.video = loadedId(); fetch('/event?t=' + encodeURIComponent(TOKEN), {method:'POST', body: JSON.stringify(o)}).catch(()=>{}); }
let player = null;
const tag = document.createElement('script');
tag.src = "https://www.youtube.com/iframe_api";
document.head.appendChild(tag);
function onYouTubeIframeAPIReady(){
  player = new YT.Player('p', {
    width: '100%', height: '100%',
    playerVars: {playsinline: 1, rel: 0, origin: location.origin},
    events: {
      onReady: () => send({ev: 'ready'}),
      onStateChange: (e) => send({ev: 'state', state: e.data}),
      onError: (e) => send({ev: 'error', code: e.data}),
    }
  });
}
function loadVideo(id, vol){ current = id; player.setVolume(vol); player.unMute(); player.loadVideoById(id); }
function pauseVideo(){ player.pauseVideo(); }
function resumeVideo(){ player.playVideo(); }
function stopVideo(){ current = ""; player.stopVideo(); }
function setVolume(v){ player.setVolume(v); }
</script></body></html>"""

# Script para la página youtube.com/watch. Corre en un mundo aislado (ApplicationWorld):
# comparte el DOM con la página, pero no sus variables. __PREFIX__ y __VOLUME__ los pone Python.
_WATCH_JS = r"""
(() => {
  if (window.__alpeka) return;
  const PREFIX = __PREFIX__;
  const TIMEOUT_MS = 30000;
  const send = (o) => console.log(PREFIX + JSON.stringify(o));
  if (location.hostname.startsWith('consent.')) { send({ev: 'consent'}); return; }
  const videoId = () => new URLSearchParams(location.search).get('v') || '';

  const st = {volume: __VOLUME__ / 100, paused: false, id: '', start: 0,
              playing: false, ended: false, failed: false};
  const reset = (id) => Object.assign(st, {id, start: Date.now(), playing: false, ended: false, failed: false});
  reset(videoId());

  // El reproductor ocupa toda la vista
  const css = document.createElement('style');
  css.textContent = `html,body{overflow:hidden!important;background:#000!important}
    #movie_player{position:fixed!important;inset:0!important;width:100vw!important;
      height:100vh!important;z-index:2147483647!important;background:#000!important}`;
  (document.head || document.documentElement).appendChild(css);
  setTimeout(() => window.dispatchEvent(new Event('resize')), 1500);

  const fail = (why) => { if (!st.failed) { st.failed = true; send({ev: 'error', video: st.id, why}); } };

  function tick() {
    const id = videoId();
    if (id !== st.id) {               // YouTube pasó a otro vídeo por su cuenta (reproducción automática)
      if (st.id && !st.ended) send({ev: 'ended', video: st.id});
      reset(id);
    }
    const mp = document.querySelector('#movie_player');
    const v = mp && mp.querySelector('video');
    const ad = mp && (mp.classList.contains('ad-showing') || mp.classList.contains('ad-interrupting'));
    if (ad) {                          // anuncio: lo silencia y salta adblock.py; no cuenta como la canción
      st.start = Date.now();           // ni para el límite de 30 s sin sonar
      return;
    }
    if (v) {
      v.loop = false;
      if (Math.abs(v.volume - st.volume) > 0.005) v.volume = st.volume;
      if (v.muted) v.muted = false;
      if (st.paused && !v.paused) v.pause();
      else if (!st.paused && v.paused && !v.ended && !st.ended) v.play().catch(() => {});
      if (!st.playing && !v.paused && v.currentTime > 0) { st.playing = true; send({ev: 'playing', video: st.id}); }
      if (v.ended && !st.ended) { st.ended = true; v.pause(); send({ev: 'ended', video: st.id}); }
    }
    const err = document.querySelector('#movie_player .ytp-error');
    if (err && err.offsetParent !== null) fail('error');
    else if (!st.playing && !st.paused && Date.now() - st.start > TIMEOUT_MS) fail('timeout');
  }
  setInterval(tick, 400);

  window.__alpeka = {
    pause() { st.paused = true; tick(); },
    resume() { st.paused = false; tick(); },
    setVolume(v) { st.volume = Math.max(0, Math.min(100, v)) / 100; tick(); },
  };
})();
"""

_SCRIPT_NAME = "alpekabot-watch"
_profile: QWebEngineProfile | None = None


def _watch_profile() -> QWebEngineProfile:
    """Perfil persistente (cookies en data/webengine): así el aviso de cookies de YouTube
    solo hay que aceptarlo o rechazarlo una vez."""
    global _profile
    if _profile is None:
        _profile = QWebEngineProfile("AlpekaBOT", QApplication.instance())
        _profile.setPersistentStoragePath(str(DATA_DIR / "webengine"))
        _profile.setCachePath(str(DATA_DIR / "webengine" / "cache"))
        _profile.setPersistentCookiesPolicy(QWebEngineProfile.AllowPersistentCookies)
        # Sin la marca "QtWebEngine/x.y" YouTube trata la vista como un Chrome normal
        _profile.setHttpUserAgent(re.sub(r"\s*QtWebEngine/\S+", "", _profile.httpUserAgent()))
    return _profile


class _ControlPage(QWebEnginePage):
    """Página que entrega a Python los mensajes de nuestros scripts (control y anuncios)
    y lleva instalado el bloqueo de anuncios."""

    def __init__(self, profile: QWebEngineProfile, on_message: Callable[[dict], None], parent=None) -> None:
        super().__init__(profile, parent)
        self.prefix = f"__alpeka_{secrets.token_hex(16)}__"
        self._on_message = on_message
        self.settings().setAttribute(QWebEngineSettings.PlaybackRequiresUserGesture, False)
        self.setBackgroundColor(QColor("black"))
        adblock.install(self, self.prefix)

    def javaScriptConsoleMessage(self, level, message, line, source) -> None:  # noqa: N802 (Qt)
        if not message.startswith(self.prefix):
            return  # mensajes de la propia web de YouTube: se ignoran
        try:
            data = json.loads(message[len(self.prefix):])
        except ValueError:
            return
        if isinstance(data, dict):
            self._on_message(data)


class WebPlayer(QObject):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._token = secrets.token_urlsafe(24)
        self._runner: web.AppRunner | None = None
        self._ready = False
        self._pending: list[str] = []

        self._embed = QWebEngineView()
        self._embed.setPage(_ControlPage(QWebEngineProfile.defaultProfile(), self._handle_ads, self._embed))

        self._watch = QWebEngineView()
        self._watch_page = _ControlPage(_watch_profile(), self._handle_watch, self._watch)
        self._watch.setPage(self._watch_page)

        self.view = QStackedWidget()  # lo que muestra la pestaña Canciones
        self.view.addWidget(self._embed)
        self.view.addWidget(self._watch)

        self._mode = "embed"           # "embed" | "watch"
        self._current = ""             # vídeo que se pidió reproducir
        self._volume = 50
        self._watch_only: set[str] = set()  # vídeos que necesitan la página de YouTube (esta sesión)
        self._consent_warned = False
        # Todos reciben el ID del vídeo al que se refiere el evento
        self.on_ended: Callable[[str], None] | None = None
        self.on_error: Callable[[int, str], None] | None = None
        self.on_playing: Callable[[str], None] | None = None

    # ---------------------------------------------------------- servidor
    async def start(self) -> None:
        app = web.Application()
        app.add_routes([web.get("/player", self._page), web.post("/event", self._event)])
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)  # solo este PC, puerto libre
        await site.start()
        port = self._runner.addresses[0][1]
        log.info("Reproductor de YouTube listo")
        self._embed.load(QUrl(f"http://127.0.0.1:{port}/player?t={self._token}"))

    async def close(self) -> None:
        self._watch.setUrl(QUrl("about:blank"))
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def _authorized(self, request: web.Request) -> bool:
        return secrets.compare_digest(request.query.get("t", ""), self._token)

    async def _page(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            raise web.HTTPForbidden()
        html = _HTML.replace("__TOKEN__", json.dumps(self._token))
        return web.Response(text=html, content_type="text/html")

    async def _event(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            raise web.HTTPForbidden()
        try:
            data = json.loads(await request.text())
        except ValueError:
            raise web.HTTPBadRequest() from None
        self._handle(data)
        return web.Response(text="ok")

    # ---------------------------------------------------------- eventos
    def _handle(self, data: dict) -> None:
        """Eventos del reproductor incrustado."""
        ev = data.get("ev")
        if ev == "ready":
            self._ready = True
            for code in self._pending:
                self._embed.page().runJavaScript(code)
            self._pending.clear()
        if self._mode != "embed":
            return  # suena en la página de YouTube: lo del incrustado ya no cuenta
        video = str(data.get("video") or "")
        if ev == "state":
            state = data.get("state")
            if state == _YT_ENDED and self.on_ended:
                self.on_ended(video)
            elif state == _YT_PLAYING and self.on_playing:
                self.on_playing(video)
        elif ev == "error":
            code = _int(data.get("code"))
            if code in EMBED_BLOCKED_CODES and video and video == self._current:
                log.info("El vídeo %s no se puede incrustar (error %s): se abre la página de YouTube", video, code)
                self._watch_only.add(video)
                self._embed_js("stopVideo()")
                self._open_watch(video)
            elif self.on_error:
                self.on_error(code, video)

    def _handle_ads(self, data: dict) -> None:
        """Avisos del plan B contra anuncios (adblock.py), de cualquiera de las dos vistas."""
        ev = data.get("ev")
        if ev == "ad":
            log.info("Se coló un anuncio de YouTube: silenciado y saltado")
        elif ev == "adwall":
            log.info("YouTube mostró el aviso de bloqueador de anuncios: cerrado")

    def _handle_watch(self, data: dict) -> None:
        """Eventos del script de control de la página de YouTube."""
        ev = data.get("ev")
        if ev in ("ad", "adwall"):
            self._handle_ads(data)
            return
        if ev == "consent":
            if not self._consent_warned:
                self._consent_warned = True
                log.warning("YouTube pide aceptar o rechazar las cookies: hazlo una vez en el "
                            "reproductor de la pestaña Canciones")
            return
        video = str(data.get("video") or "")
        if self._mode != "watch" or not video or video != self._current:
            return  # evento atrasado o de otro vídeo
        if ev == "playing" and self.on_playing:
            self.on_playing(video)
        elif ev == "ended" and self.on_ended:
            self.on_ended(video)
        elif ev == "error":
            log.warning("Tampoco se pudo reproducir %s en la página de YouTube (%s)", video, data.get("why"))
            self._watch_only.discard(video)
            if self.on_error:
                self.on_error(WATCH_FAILED_CODE, video)

    # ---------------------------------------------------------- modos
    def _open_watch(self, video_id: str) -> None:
        self._mode = "watch"
        scripts = self._watch_page.scripts()
        for old in scripts.find(_SCRIPT_NAME):
            scripts.remove(old)
        script = QWebEngineScript()
        script.setName(_SCRIPT_NAME)
        script.setInjectionPoint(QWebEngineScript.DocumentReady)
        script.setWorldId(QWebEngineScript.ApplicationWorld)
        script.setRunsOnSubFrames(False)
        script.setSourceCode(_WATCH_JS.replace("__PREFIX__", json.dumps(self._watch_page.prefix))
                             .replace("__VOLUME__", str(int(self._volume))))
        scripts.insert(script)
        self.view.setCurrentWidget(self._watch)
        self._watch.load(QUrl(f"https://www.youtube.com/watch?v={video_id}"))

    def _close_watch(self) -> None:
        if self._mode == "watch":
            self._mode = "embed"
            self._watch.setUrl(QUrl("about:blank"))
            self.view.setCurrentWidget(self._embed)

    def _watch_js(self, code: str) -> None:
        self._watch_page.runJavaScript(f"window.__alpeka && window.__alpeka.{code}",
                                       QWebEngineScript.ApplicationWorld)

    def _embed_js(self, code: str) -> None:
        if self._ready:
            self._embed.page().runJavaScript(code)
        else:
            self._pending.append(code)

    # ---------------------------------------------------------- controles
    def load(self, video_id: str, volume: int) -> None:
        self._current = video_id
        self._volume = int(volume)
        if video_id in self._watch_only:
            self._open_watch(video_id)
        else:
            self._close_watch()
            self._embed_js(f"loadVideo({json.dumps(video_id)}, {int(volume)})")

    def pause(self) -> None:
        if self._mode == "watch":
            self._watch_js("pause()")
        else:
            self._embed_js("pauseVideo()")

    def resume(self) -> None:
        if self._mode == "watch":
            self._watch_js("resume()")
        else:
            self._embed_js("resumeVideo()")

    def stop(self) -> None:
        self._current = ""
        self._close_watch()
        self._embed_js("stopVideo()")

    def query_progress(self, callback: Callable[[float, float], None]) -> None:
        """Pide (segundos actuales, duración) al reproductor; ``callback`` se llama cuando responde."""
        # Ojo: QtWebEngine no convierte los arrays de JS (llegan como ''): se devuelve JSON en texto
        def done(result) -> None:
            try:
                pos, dur = json.loads(result) if isinstance(result, str) and result else (None, None)
                if pos is not None:
                    callback(float(pos), float(dur or 0))
            except (TypeError, ValueError):
                pass

        if not self._current:
            return
        if self._mode == "watch":
            self._watch_page.runJavaScript(
                "(() => { const v = document.querySelector('video.html5-main-video') || document.querySelector('video');"
                " const ad = document.querySelector('.ad-showing');"
                " return v && !ad ? JSON.stringify([v.currentTime, v.duration || 0]) : ''; })()",
                QWebEngineScript.ApplicationWorld, done,
            )
        elif self._ready:
            self._embed.page().runJavaScript(
                "(typeof player !== 'undefined' && player && player.getCurrentTime)"
                " ? JSON.stringify([player.getCurrentTime(), player.getDuration()]) : ''",
                0, done,
            )

    def set_volume(self, volume: int) -> None:
        self._volume = int(volume)
        if self._mode == "watch":
            self._watch_js(f"setVolume({int(volume)})")
        else:
            self._embed_js(f"setVolume({int(volume)})")


_YT_ENDED, _YT_PLAYING = 0, 1


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1
