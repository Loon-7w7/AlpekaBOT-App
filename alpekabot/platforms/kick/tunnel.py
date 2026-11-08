"""Túnel de ngrok integrado (librería oficial ``ngrok``): dominio estático → 127.0.0.1:<puerto>."""

from __future__ import annotations

import logging

import ngrok

from ..base import PlatformError

log = logging.getLogger(__name__)


def normalize_domain(raw: str) -> str:
    """'https://mi-bot.ngrok-free.app/' -> 'mi-bot.ngrok-free.app'."""
    d = raw.strip().lower()
    for prefix in ("https://", "http://"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    return d.split("/")[0]


class NgrokTunnel:
    def __init__(self) -> None:
        self._session = None
        self._listener = None
        self.url: str | None = None

    async def start(self, authtoken: str, domain: str, local_port: int) -> str:
        if not authtoken or not domain:
            raise PlatformError("Falta el authtoken o el dominio de ngrok (Configuración → Kick).")
        try:
            self._session = await ngrok.SessionBuilder().authtoken(authtoken).connect()
            self._listener = await (
                self._session.http_endpoint().domain(domain).listen_and_forward(f"http://127.0.0.1:{local_port}")
            )
        except Exception as exc:
            await self.close()
            # El mensaje de ngrok no incluye el authtoken (verificado), pero por si acaso se elimina
            detail = " ".join(str(a) for a in getattr(exc, "args", ()) or [str(exc)]).replace(authtoken, "***")
            log.warning("ngrok no pudo abrir el túnel: %s", detail[:300])
            raise PlatformError(f"ngrok no pudo abrir el túnel: {detail[:200]}") from None
        self.url = self._listener.url()
        log.info("Túnel ngrok abierto: %s", self.url)
        return self.url

    async def close(self) -> None:
        for obj in (self._listener, self._session):
            if obj is not None:
                try:
                    await obj.close()
                except Exception:
                    log.debug("Error cerrando ngrok", exc_info=True)
        self._listener = self._session = None
        self.url = None
