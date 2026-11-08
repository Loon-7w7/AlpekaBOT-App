"""Receptor de webhooks de Kick.

El servidor escucha SOLO en 127.0.0.1 y el túnel de ngrok le reenvía lo que llega
a tu dominio público. Como esa URL es pública, cada petición se valida:

1. Firma RSA-SHA256 (PKCS#1 v1.5) de Kick sobre "<message_id>.<timestamp>.<body>",
   verificada con la clave pública de GET /public-key (se recarga una vez si falla,
   por si Kick la ha rotado).
2. Antigüedad máxima del evento (evita reenvíos de eventos viejos).
3. Duplicados por Kick-Event-Message-Id (Kick puede reenviar el mismo evento).
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import time
from collections import OrderedDict
from datetime import datetime
from typing import Awaitable, Callable

from aiohttp import web
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import load_pem_public_key

log = logging.getLogger(__name__)

WEBHOOK_PATH = "/kick/webhook"
MAX_AGE_S = 600
MAX_BODY = 256 * 1024
DEDUP_SIZE = 2000

EventHandler = Callable[[str, int, dict], None]


def _load_key(pem: str) -> rsa.RSAPublicKey:
    key = load_pem_public_key(pem.encode("ascii"))
    if not isinstance(key, rsa.RSAPublicKey):
        raise ValueError("La clave pública de Kick no es RSA")
    return key


def signature_ok(key: rsa.RSAPublicKey, message_id: str, timestamp: str, body: bytes, signature_b64: str) -> bool:
    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except (binascii.Error, ValueError):
        return False
    signed = f"{message_id}.{timestamp}.".encode() + body
    try:
        key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
        return True
    except InvalidSignature:
        return False


class SignatureVerifier:
    def __init__(self, fetch_pem: Callable[[], Awaitable[str]]) -> None:
        self._fetch_pem = fetch_pem
        self._key: rsa.RSAPublicKey | None = None

    async def load(self) -> None:
        self._key = _load_key(await self._fetch_pem())

    async def verify(self, message_id: str, timestamp: str, body: bytes, signature_b64: str) -> bool:
        if self._key is None:
            await self.load()
        if signature_ok(self._key, message_id, timestamp, body, signature_b64):
            return True
        # ¿Kick rotó la clave? Se recarga UNA vez y se reintenta.
        try:
            await self.load()
        except Exception:
            log.warning("No se pudo recargar la clave pública de Kick", exc_info=True)
            return False
        return signature_ok(self._key, message_id, timestamp, body, signature_b64)


def _parse_ts(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class WebhookServer:
    def __init__(
        self,
        verifier: SignatureVerifier,
        on_event: EventHandler,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._verifier = verifier
        self._on_event = on_event
        self._clock = clock
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._runner: web.AppRunner | None = None
        self.port: int | None = None

    def make_app(self) -> web.Application:
        app = web.Application(client_max_size=MAX_BODY)
        app.router.add_post(WEBHOOK_PATH, self.handle)
        return app

    async def start(self, port: int = 0) -> int:
        self._runner = web.AppRunner(self.make_app(), access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", port).start()
        self.port = self._runner.addresses[0][1]
        return self.port

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def handle(self, request: web.Request) -> web.Response:
        h = request.headers
        msg_id = h.get("Kick-Event-Message-Id", "")
        ts = h.get("Kick-Event-Message-Timestamp", "")
        sig = h.get("Kick-Event-Signature", "")
        ev_type = h.get("Kick-Event-Type", "")
        if not (msg_id and ts and sig and ev_type):
            return web.Response(status=400, text="faltan cabeceras")
        sent_at = _parse_ts(ts)
        if sent_at is None or abs(self._clock() - sent_at) > MAX_AGE_S:
            log.warning("Webhook de Kick rechazado: marca de tiempo fuera de rango")
            return web.Response(status=400, text="timestamp")
        body = await request.read()
        if not await self._verifier.verify(msg_id, ts, body, sig):
            log.warning("Webhook de Kick rechazado: firma no válida")
            return web.Response(status=401, text="firma")
        if msg_id in self._seen:
            return web.Response(text="duplicado")
        self._seen[msg_id] = None
        while len(self._seen) > DEDUP_SIZE:
            self._seen.popitem(last=False)
        try:
            payload = json.loads(body)
            version = int(h.get("Kick-Event-Version", "1"))
        except ValueError:
            return web.Response(status=400, text="json")
        try:
            self._on_event(ev_type, version, payload)
        except Exception:
            log.exception("Error procesando el evento %s de Kick", ev_type)
        return web.Response(text="ok")
