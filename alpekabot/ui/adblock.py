"""Quita los anuncios de YouTube del reproductor de canciones, en tres capas.

1. **Respuesta sin anuncios** (``_PRUNE_JS``, mundo principal, antes que el código de
   YouTube): borra ``adPlacements``/``adSlots``/``playerAds`` de la respuesta del
   reproductor (``ytInitialPlayerResponse``, ``JSON.parse`` y ``Response.json``). Sin
   esos datos el reproductor no sabe que tiene anuncios.
2. **Filtro de red** (``AdBlockInterceptor``): bloquea las peticiones de anuncios y su
   rastreo (doubleclick, ``/pagead/``, ``/api/stats/ads``...).
3. **Plan B** (``_GUARD_JS``, mundo aislado): si aun así aparece un anuncio, se silencia,
   se acelera ×16, se salta al final y se pulsa «Omitir». También cierra el aviso de
   «bloqueadores de anuncios no permitidos». Avisa a Python por ``console.log`` con el
   prefijo secreto de la página.

YouTube cambia esto a menudo: si las capas 1 y 2 dejan de servir, la 3 evita que el
anuncio se oiga en el directo.
"""

from __future__ import annotations

import json

from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineScript, QWebEngineUrlRequestInterceptor

# Dominios que solo sirven anuncios (también sus subdominios)
_AD_HOSTS = (
    "doubleclick.net",
    "googlesyndication.com",
    "googleadservices.com",
    "googletagservices.com",
    "adservice.google.com",
    "imasdk.googleapis.com",
)
# Rutas de anuncios dentro de youtube.com
_AD_PATHS = (
    "/pagead/",
    "/api/stats/ads",
    "/ptracking",
    "/get_midroll_info",
    "/youtubei/v1/player/ad_break",
)
_YOUTUBE_HOSTS = ("youtube.com", "youtube-nocookie.com")


def _matches(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def is_ad_request(host: str, path: str) -> bool:
    """¿Es una petición de anuncios? Función pura (se llama desde el hilo de red de Qt)."""
    host = (host or "").lower().rstrip(".")
    path = path or ""
    if _matches(host, _AD_HOSTS):
        return True
    return _matches(host, _YOUTUBE_HOSTS) and path.startswith(_AD_PATHS)


class AdBlockInterceptor(QWebEngineUrlRequestInterceptor):
    def interceptRequest(self, info) -> None:  # noqa: N802 (Qt)
        url = info.requestUrl()
        if is_ad_request(url.host(), url.path()):
            info.block(True)


_YT_HOST_RE = r"/(^|\.)youtube(-nocookie)?\.com$/"

# Mundo principal y antes que cualquier script de la página: tiene que envolver
# JSON.parse y fetch antes de que YouTube los use. No usa console.log (la página
# podría leer el prefijo secreto).
_PRUNE_JS = r"""
(() => {
  if (!__HOST_RE__.test(location.hostname)) return;
  const KEYS = ['adPlacements', 'adSlots', 'playerAds', 'adBreakHeartbeatParams'];
  const prune = (o, depth) => {
    if (!o || typeof o !== 'object' || depth > 3) return o;
    if (Array.isArray(o)) { for (const x of o) prune(x, depth + 1); return o; }
    for (const k of KEYS) if (k in o) delete o[k];
    if (o.playerResponse) prune(o.playerResponse, depth + 1);
    return o;
  };
  const safe = (r) => { try { prune(r, 0); } catch (e) {} return r; };
  // Proxy: la función sigue pareciendo nativa (toString) para el código de YouTube
  const wrap = (obj, name, after) => {
    const orig = obj && obj[name];
    if (typeof orig !== 'function') return;
    obj[name] = new Proxy(orig, {apply: (t, self, args) => after(Reflect.apply(t, self, args))});
  };
  wrap(JSON, 'parse', safe);
  if (window.Response) wrap(Response.prototype, 'json', (p) => p.then(safe));
  let initial;
  try {
    Object.defineProperty(window, 'ytInitialPlayerResponse', {
      configurable: true, get: () => initial, set: (v) => { initial = safe(v); },
    });
  } catch (e) {}
})();
"""

# Mundo aislado (comparte el DOM, no las variables de la página). __PREFIX__ lo pone Python.
_GUARD_JS = r"""
(() => {
  if (window.__alpekaAds || !__HOST_RE__.test(location.hostname)) return;
  window.__alpekaAds = true;
  const PREFIX = __PREFIX__;
  const send = (o) => console.log(PREFIX + JSON.stringify(o));
  let inAd = false, mutedByUs = false, wallSent = false;
  setInterval(() => {
    const mp = document.querySelector('.html5-video-player');
    const v = mp && mp.querySelector('video');
    const ad = !!mp && (mp.classList.contains('ad-showing') || mp.classList.contains('ad-interrupting'));
    if (ad && v) {
      if (!inAd) { inAd = true; send({ev: 'ad'}); }
      if (!v.muted) { v.muted = true; mutedByUs = true; }
      if (v.playbackRate < 16) v.playbackRate = 16;
      if (isFinite(v.duration) && v.duration > 0 && v.currentTime < v.duration - 0.3) v.currentTime = v.duration - 0.1;
      const skip = document.querySelector('.ytp-skip-ad-button, .ytp-ad-skip-button, .ytp-ad-skip-button-modern');
      if (skip) skip.click();
    } else if (inAd) {
      inAd = false;
      if (v) { v.playbackRate = 1; if (mutedByUs) v.muted = false; }
      mutedByUs = false;
    }
    const wall = document.querySelector('ytd-enforcement-message-view-model');
    if (wall) {   // «Los bloqueadores de anuncios no están permitidos»
      (wall.closest('tp-yt-paper-dialog') || wall).remove();
      const backdrop = document.querySelector('tp-yt-iron-overlay-backdrop');
      if (backdrop) backdrop.remove();
      if (v && v.paused) v.play().catch(() => {});
      if (!wallSent) { wallSent = true; send({ev: 'adwall'}); }
    }
  }, 250);
})();
"""

_PRUNE_NAME = "alpekabot-adblock-prune"
_GUARD_NAME = "alpekabot-adblock-guard"


def _script(name: str, source: str, world: int) -> QWebEngineScript:
    script = QWebEngineScript()
    script.setName(name)
    script.setInjectionPoint(QWebEngineScript.DocumentCreation)
    script.setWorldId(world)
    script.setRunsOnSubFrames(True)  # el reproductor incrustado es un iframe de youtube.com
    script.setSourceCode(source.replace("__HOST_RE__", _YT_HOST_RE))
    return script


def install(page: QWebEnginePage, prefix: str) -> None:
    """Instala las tres capas en ``page``. Los avisos del plan B llegan a su
    ``javaScriptConsoleMessage`` con ``prefix`` delante: ``{"ev": "ad"}`` o ``{"ev": "adwall"}``."""
    scripts = page.scripts()
    for name in (_PRUNE_NAME, _GUARD_NAME):
        for old in scripts.find(name):
            scripts.remove(old)
    scripts.insert(_script(_PRUNE_NAME, _PRUNE_JS, QWebEngineScript.MainWorld))
    scripts.insert(_script(_GUARD_NAME, _GUARD_JS.replace("__PREFIX__", json.dumps(prefix)),
                           QWebEngineScript.ApplicationWorld))
    interceptor = AdBlockInterceptor(page)  # hijo de la página: vive lo mismo que ella
    page.setUrlRequestInterceptor(interceptor)
