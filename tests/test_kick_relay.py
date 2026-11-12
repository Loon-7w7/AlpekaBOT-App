"""Kick con la app compartida: conexión con el intermediario (Worker) por WebSocket."""

import asyncio
import json
import time

import pytest
from aiohttp import web

from alpekabot.config import KickConfig
from alpekabot.core.models import AccountRole, Platform
from alpekabot.platforms.base import PlatformError
from alpekabot.platforms.kick import api as kick_api
from alpekabot.platforms.kick import auth, relay
from alpekabot.platforms.kick.client import EVENTS, KickClient
from alpekabot.platforms.kick.relay import RelayConnection, RelayNotAllowed
from test_kick import chat_payload, new_key, now_rfc3339, sign


async def _no_sleep(_s):
    await asyncio.sleep(0)


class FakeRelay:
    """Imita el Worker: GET /ws con Bearer; 401/403 según el token; empuja eventos."""

    def __init__(self, tokens: dict[str, int]):
        self.tokens = tokens            # token -> estado HTTP (101 = aceptar)
        self.sockets: list[web.WebSocketResponse] = []
        self.connects: list[str] = []
        app = web.Application()
        app.router.add_get("/ws", self._ws)
        self.runner = web.AppRunner(app)

    async def start(self) -> str:
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        return f"http://127.0.0.1:{self.runner.addresses[0][1]}/"

    async def stop(self):
        await self.runner.cleanup()

    async def _ws(self, request):
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        self.connects.append(token)
        status = self.tokens.get(token, 401)
        if status != 101:
            return web.json_response({"error": "x"}, status=status)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.sockets.append(ws)
        async for _ in ws:
            pass
        return ws

    async def push(self, headers: dict, body: str):
        for ws in list(self.sockets):
            if not ws.closed:
                await ws.send_str(json.dumps({"type": "event", "headers": headers, "body": body}))

    async def wait_sockets(self, n: int):
        for _ in range(200):
            if len([w for w in self.sockets if not w.closed]) >= n:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("el cliente no se conectó")


class Tokens:
    def __init__(self, token="ok"):
        self.token, self.refreshed = token, 0

    async def access_token(self):
        return self.token

    async def refresh(self):
        self.refreshed += 1
        self.token = "ok"


async def _wait(cond):
    for _ in range(200):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("no llegó a tiempo")


def test_recibe_eventos_con_cabeceras_y_cuerpo_intactos():
    async def main():
        fake = FakeRelay({"ok": 101})
        base = await fake.start()
        got = []

        async def sink(headers, body):
            got.append((headers, body))

        conn = RelayConnection(base, Tokens(), sink)
        await conn.start()
        await fake.wait_sockets(1)
        await fake.push({"kick-event-type": "chat.message.sent"}, '{"a":"ñ"}')
        await _wait(lambda: got)
        await conn.close()
        await fake.stop()
        return got

    headers, body = asyncio.run(main())[0]
    assert headers == {"kick-event-type": "chat.message.sent"}
    assert body == '{"a":"ñ"}'.encode("utf-8")


def test_canal_no_autorizado():
    async def main():
        fake = FakeRelay({"ok": 403})
        base = await fake.start()
        conn = RelayConnection(base, Tokens(), None, channel="loon_vt")
        try:
            await conn.start()
        finally:
            await conn.close()
            await fake.stop()

    with pytest.raises(RelayNotAllowed, match="loon_vt"):
        asyncio.run(main())


def test_token_caducado_se_renueva_una_vez():
    async def main():
        fake = FakeRelay({"viejo": 401, "ok": 101})
        base = await fake.start()
        tokens = Tokens("viejo")
        conn = RelayConnection(base, tokens, None)
        await conn.start()
        await conn.close()
        await fake.stop()
        return tokens.refreshed, fake.connects

    assert asyncio.run(main()) == (1, ["viejo", "ok"])


def test_servidor_caido_da_error_claro():
    async def main():
        conn = RelayConnection("http://127.0.0.1:9/", Tokens(), None)
        try:
            await conn.start()
        finally:
            await conn.close()

    with pytest.raises(PlatformError, match="servidor de AlpekaBOT"):
        asyncio.run(main())


def test_reconecta_si_se_cae_la_conexion():
    async def main():
        fake = FakeRelay({"ok": 101})
        base = await fake.start()
        got = []

        async def sink(headers, body):
            got.append(body)

        conn = RelayConnection(base, Tokens(), sink, sleep=_no_sleep)
        await conn.start()
        await fake.wait_sockets(1)
        await fake.sockets[0].close()           # el Worker corta
        await fake.wait_sockets(1)              # ...y el cliente vuelve solo
        await fake.push({}, "otra vez")
        await _wait(lambda: got)
        await conn.close()
        await fake.stop()
        return len(fake.connects), got

    connects, got = asyncio.run(main())
    assert connects == 2 and got == [b"otra vez"]


# ------------------------------------------- flujo completo con la app compartida
def test_flujo_completo_app_compartida(monkeypatch):
    """connect (sin ngrok) → suscripciones → evento firmado por el intermediario → chat."""
    priv, pem = new_key()
    created, deleted = [], []
    users = {"Bearer bot-tok": {"user_id": 1, "name": "AlpekaBOT"},
             "Bearer str-tok": {"user_id": 100, "name": "loon_vt"}}

    async def h_users(r): return web.json_response({"data": [users[r.headers["Authorization"]]]})
    async def h_key(r): return web.json_response({"data": {"public_key": pem}})
    async def h_list(r): return web.json_response({"data": []})
    async def h_create(r):
        body = await r.json(); created.append(body)
        return web.json_response({"data": [{"name": e["name"], "subscription_id": "s-" + e["name"]}
                                           for e in body["events"]]})
    async def h_delete(r):
        deleted.extend(r.query.getall("id")); return web.Response(status=204)
    async def h_channels(r):
        return web.json_response({"data": [{"slug": "loon-vt", "stream": {"is_live": False}}]})

    kick = web.Application()
    kick.router.add_get("/public/v1/users", h_users)
    kick.router.add_get("/public/v1/public-key", h_key)
    kick.router.add_get("/public/v1/events/subscriptions", h_list)
    kick.router.add_post("/public/v1/events/subscriptions", h_create)
    kick.router.add_delete("/public/v1/events/subscriptions", h_delete)
    kick.router.add_get("/public/v1/channels", h_channels)

    json_store = {
        "kick/account/bot": {"access_token": "bot-tok", "refresh_token": "r", "expires_at": time.time() + 3600,
                             "user_id": "1", "login": "alpekabot", "display_name": "AlpekaBOT"},
        "kick/account/streamer": {"access_token": "str-tok", "refresh_token": "r", "expires_at": time.time() + 3600,
                                  "user_id": "100", "login": "loon_vt", "display_name": "loon_vt",
                                  "scopes": list(auth.SCOPES[AccountRole.STREAMER])},
    }
    monkeypatch.setattr(auth.credentials, "get_json", lambda k: json_store.get(k))
    monkeypatch.setattr(auth.credentials, "get_secret", lambda k: None)  # sin secreto ni ngrok

    async def main():
        runner = web.AppRunner(kick)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        monkeypatch.setattr(kick_api, "API_BASE", f"http://127.0.0.1:{runner.addresses[0][1]}/public/v1/")
        fake = FakeRelay({"str-tok": 101})
        monkeypatch.setattr(relay, "RELAY_BASE", await fake.start())

        from conftest import FakeBot
        bot = FakeBot()
        messages = []
        bot.bus.chat_message.connect(messages.append)
        client = KickClient(KickConfig(), bot.bus)   # por defecto: app compartida
        assert client.is_configured

        async def no_ngrok(*a):
            raise AssertionError("con la app compartida no se usa ngrok")
        monkeypatch.setattr(client._tunnel, "start", no_ngrok)

        await client.connect()
        assert client.is_connected and client._webhook.port is None   # sin servidor local
        assert {e["name"] for e in created[0]["events"]} == set(EVENTS)
        await fake.wait_sockets(1)

        body = json.dumps(chat_payload(content="hola"))
        ts = now_rfc3339()
        good = {"kick-event-message-id": "w1", "kick-event-message-timestamp": ts,
                "kick-event-signature": sign(priv, "w1", ts, body.encode()),
                "kick-event-type": "chat.message.sent", "kick-event-version": "1"}
        forged = dict(good, **{"kick-event-signature": sign(priv, "w1", ts, b"otro")})
        await fake.push(forged, body)   # firma que no corresponde: se descarta
        await fake.push(good, body)
        await _wait(lambda: messages)
        await asyncio.sleep(0.05)

        await client.disconnect()
        await fake.stop()
        await runner.cleanup()
        return messages

    messages = asyncio.run(main())
    assert len(messages) == 1
    assert messages[0].platform is Platform.KICK and messages[0].text == "hola"
    assert sorted(deleted) == sorted("s-" + e for e in EVENTS)
