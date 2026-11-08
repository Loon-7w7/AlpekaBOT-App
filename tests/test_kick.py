import asyncio
import base64
import hashlib
import json
import socket
import time
from datetime import datetime, timezone

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from alpekabot.config import AppConfig, KickConfig
from alpekabot.core.event_bus import EventBus
from alpekabot.core.models import AccountInfo, AccountRole, EventKind, Platform
from alpekabot.platforms.base import PlatformError
from alpekabot.platforms.kick import auth
from alpekabot.platforms.kick.api import KickUnauthorized
from alpekabot.platforms.kick.client import (
    KickClient,
    channel_event_from_payload,
    chat_message_from_payload,
    strip_emotes,
)
from alpekabot.platforms.kick.tunnel import normalize_domain
from alpekabot.platforms.kick.webhook import WEBHOOK_PATH, SignatureVerifier, WebhookServer, signature_ok


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------- utilidades
def new_key():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return priv, pem


def sign(priv, msg_id: str, ts: str, body: bytes) -> str:
    sig = priv.sign(f"{msg_id}.{ts}.".encode() + body, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode()


def now_rfc3339() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def chat_payload(**over):
    p = {
        "message_id": "m-1",
        "broadcaster": {"user_id": 100, "username": "loon_vt"},
        "sender": {"user_id": 200, "username": "Pepe",
                   "identity": {"username_color": "#FF5733",
                                "badges": [{"text": "Moderator", "type": "moderator"},
                                           {"text": "Sub Gifter", "type": "sub_gifter", "count": 5}]}},
        "content": "hola [emote:37226:KEKW] !sr algo",
        "replies_to": {"message_id": "m-0"},
        "created_at": "2025-01-14T16:08:06Z",
    }
    p.update(over)
    return p


# ------------------------------------------------------------- firma RSA
def test_firma_valida_y_manipulada():
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    priv, pem = new_key()
    key = load_pem_public_key(pem.encode())
    ts, body = now_rfc3339(), b'{"a":1}'
    sig = sign(priv, "id1", ts, body)
    assert signature_ok(key, "id1", ts, body, sig)
    assert not signature_ok(key, "id1", ts, b'{"a":2}', sig)        # cuerpo cambiado
    assert not signature_ok(key, "id2", ts, body, sig)              # id cambiado
    assert not signature_ok(key, "id1", ts, body, "no-es-base64!!")


def test_verificador_recarga_la_clave_si_kick_la_rota():
    priv_a, pem_a = new_key()
    priv_b, pem_b = new_key()
    pems = [pem_a, pem_b]
    calls = []

    async def fetch():
        calls.append(1)
        return pems[min(len(calls) - 1, 1)]

    v = SignatureVerifier(fetch)
    ts, body = now_rfc3339(), b"{}"
    assert run(v.verify("x", ts, body, sign(priv_b, "x", ts, body)))  # firmado con la nueva
    assert len(calls) == 2


# ---------------------------------------------------- servidor de webhooks
class Harness:
    def __init__(self):
        self.priv, self.pem = new_key()
        self.events = []

        async def fetch():
            return self.pem

        self.server = WebhookServer(SignatureVerifier(fetch), lambda t, v, p: self.events.append((t, v, p)))

    def headers(self, body: bytes, msg_id="id-1", ts=None, sig=None, ev="chat.message.sent"):
        ts = ts or now_rfc3339()
        return {
            "Kick-Event-Message-Id": msg_id, "Kick-Event-Message-Timestamp": ts,
            "Kick-Event-Signature": sig or sign(self.priv, msg_id, ts, body),
            "Kick-Event-Type": ev, "Kick-Event-Version": "1", "Content-Type": "application/json",
        }


async def _post_all(h: Harness, requests):
    async with TestClient(TestServer(h.server.make_app())) as client:
        out = []
        for path, body, headers in requests:
            r = await client.post(path, data=body, headers=headers)
            out.append(r.status)
        return out


def test_webhook_acepta_firmado_y_descarta_duplicado():
    h = Harness()
    body = json.dumps(chat_payload()).encode()
    hd = h.headers(body)
    assert run(_post_all(h, [(WEBHOOK_PATH, body, hd), (WEBHOOK_PATH, body, hd)])) == [200, 200]
    assert len(h.events) == 1 and h.events[0][0] == "chat.message.sent"


def test_webhook_rechaza_firma_falsa_viejo_y_sin_cabeceras():
    h = Harness()
    body = b'{"x":1}'
    otro, _ = new_key()
    viejo = datetime.fromtimestamp(time.time() - 3600, timezone.utc).isoformat()
    falsa = h.headers(body)
    falsa["Kick-Event-Signature"] = sign(otro, "id-1", falsa["Kick-Event-Message-Timestamp"], body)
    statuses = run(_post_all(h, [
        (WEBHOOK_PATH, body, falsa),
        (WEBHOOK_PATH, body, h.headers(body, msg_id="id-2", ts=viejo)),
        (WEBHOOK_PATH, body, {"Content-Type": "application/json"}),
        ("/otra-ruta", body, h.headers(body, msg_id="id-3")),
    ]))
    assert statuses[:3] == [401, 400, 400] and statuses[3] in (404, 405)
    assert h.events == []


# --------------------------------------------------------- mensajes de chat
def test_payload_a_chat_message():
    m = chat_message_from_payload(chat_payload())
    assert m.platform is Platform.KICK and m.user_name == "Pepe" and m.user_login == "pepe"
    assert m.text == "hola KEKW !sr algo"
    assert m.badges == ("moderator",) and m.color == "#FF5733" and m.reply_to_id == "m-0"
    assert m.channel_id == "100" and m.user_id == "200"


def test_el_streamer_tiene_insignia_de_broadcaster():
    p = chat_payload(sender={"user_id": 100, "username": "loon_vt", "identity": None})
    assert chat_message_from_payload(p).badges == ("broadcaster",)


def test_strip_emotes():
    assert strip_emotes("[emote:1:A] y [emote:22:B_c]") == "A y B_c"


def _client_con_streamer():
    bus = EventBus()
    c = KickClient(KickConfig(client_id="x"), bus)
    c.streamer = AccountInfo(Platform.KICK, AccountRole.STREAMER, "100", "loon_vt", "loon_vt")
    got = {"chat": [], "stream": []}
    bus.chat_message.connect(got["chat"].append)
    bus.stream_status.connect(got["stream"].append)
    return c, got


def test_eventos_del_canal():
    c, got = _client_con_streamer()
    c._on_event("chat.message.sent", 1, chat_payload())
    c._on_event("chat.message.sent", 1, chat_payload(broadcaster={"user_id": 999}))  # otro canal
    assert len(got["chat"]) == 1
    c._on_event("livestream.status.updated", 1, {"broadcaster": {"user_id": 100}, "is_live": True,
                                                  "title": "Directo", "started_at": "2025-01-01T11:00:00+11:00"})
    assert c.stream_info.is_live and c.stream_info.title == "Directo" and c.stream_info.started_at is not None
    c._on_event("livestream.metadata.updated", 1, {"broadcaster": {"user_id": 100},
                                                    "metadata": {"title": "Nuevo", "category": {"name": "Minecraft"}}})
    assert c.stream_info.is_live and c.stream_info.title == "Nuevo" and c.stream_info.game == "Minecraft"
    c._on_event("livestream.status.updated", 1, {"broadcaster": {"user_id": 100}, "is_live": False})
    assert not c.stream_info.is_live and c.stream_info.uptime_text == "offline"


# ----------------------------------------------- eventos para las reacciones
PEPE = {"user_id": 7, "username": "pepe"}


def test_payload_follow_y_subs():
    ev = channel_event_from_payload("channel.followed", {"follower": PEPE})
    assert (ev.platform, ev.kind, ev.user_id, ev.user_name) == (Platform.KICK, EventKind.FOLLOW, "7", "pepe")
    ev = channel_event_from_payload("channel.subscription.new", {"subscriber": PEPE, "duration": 1})
    assert (ev.kind, ev.amount) == (EventKind.SUB, 1)
    ev = channel_event_from_payload("channel.subscription.renewal", {"subscriber": PEPE, "duration": 6})
    assert (ev.kind, ev.amount) == (EventKind.RESUB, 6)


def test_payload_regalo_es_un_solo_evento_con_la_cantidad():
    ev = channel_event_from_payload("channel.subscription.gifts", {"gifter": PEPE, "giftees": [PEPE, PEPE, PEPE]})
    assert (ev.kind, ev.user_name, ev.amount) == (EventKind.GIFT, "pepe", 3)


def test_payload_regalo_anonimo():
    ev = channel_event_from_payload("channel.subscription.gifts",
                                    {"gifter": {"is_anonymous": True, "user_id": None}, "giftees": [PEPE]})
    assert (ev.user_id, ev.user_name) == ("", "Anónimo")


def test_payload_kicks():
    ev = channel_event_from_payload("kicks.gifted", {"sender": PEPE, "gift": {"amount": 250, "name": "Rayo"}})
    assert (ev.kind, ev.user_name, ev.amount) == (EventKind.BITS, "pepe", 250)
    assert channel_event_from_payload("kicks.gifted", {"sender": PEPE}).amount == 0


def _canje(status="pending", **over):
    return {"id": "c1", "user_input": "hola", "status": status, "redeemer": PEPE,
            "reward": {"id": "r1", "title": "Saludo", "cost": 100}, **over}


def test_payload_canje():
    ev = channel_event_from_payload("channel.reward.redemption.updated", _canje())
    assert (ev.kind, ev.reward_id, ev.reward_title, ev.redemption_id) == (EventKind.REWARD, "r1", "Saludo", "c1")
    assert (ev.user_name, ev.amount, ev.user_input, ev.pending) == ("pepe", 100, "hola", True)
    assert not channel_event_from_payload("channel.reward.redemption.updated", _canje("accepted")).pending


@pytest.mark.parametrize("payload", [_canje("rejected"), _canje(id=None), _canje(reward={}), {}])
def test_payload_canje_invalido_o_rechazado(payload):
    assert channel_event_from_payload("channel.reward.redemption.updated", payload) is None


def test_payload_de_otro_tipo_se_ignora():
    assert channel_event_from_payload("moderation.banned", {"x": 1}) is None


def test_el_cliente_emite_y_no_repite_el_mismo_canje():
    c, _ = _client_con_streamer()
    events = []
    c._bus.channel_event.connect(events.append)
    canal = {"broadcaster": {"user_id": 100}}
    c._on_event("channel.reward.redemption.updated", 1, {**_canje("pending"), **canal})
    c._on_event("channel.reward.redemption.updated", 1, {**_canje("accepted"), **canal})  # al aceptarlo nosotros
    c._on_event("channel.followed", 1, {"follower": PEPE, **canal})
    assert [e.kind for e in events] == [EventKind.REWARD, EventKind.FOLLOW]
    assert events[0].pending


def test_el_cliente_ignora_eventos_de_otro_canal():
    c, _ = _client_con_streamer()
    events = []
    c._bus.channel_event.connect(events.append)
    c._on_event("channel.followed", 1, {"follower": PEPE, "broadcaster": {"user_id": 999}})
    assert events == []


# --------------------------------------------------------------- OAuth/PKCE
def test_pkce_s256():
    verifier, challenge = auth.make_pkce()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected


def test_authorize_url():
    url = auth.build_authorize_url("cid", "http://localhost:17564/callback", ["user:read", "chat:write"], "st", "ch")
    assert url.startswith("https://id.kick.com/oauth/authorize?")
    assert "scope=user%3Aread+chat%3Awrite" in url and "code_challenge_method=S256" in url and "state=st" in url


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_callback_valida_state():
    port = _free_port()
    redirect = f"http://localhost:{port}/callback"

    async def main():
        task = asyncio.ensure_future(auth.wait_for_code(redirect, "estado-bueno", timeout=10))
        await asyncio.sleep(0.3)
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://127.0.0.1:{port}/callback?code=MAL&state=otro") as r:
                assert r.status == 400
            async with s.get(f"http://127.0.0.1:{port}/callback?code=BUENO&state=estado-bueno") as r:
                assert r.status == 200
        return await task

    assert run(main()) == "BUENO"


def test_callback_cancelado():
    port = _free_port()

    async def main():
        task = asyncio.ensure_future(auth.wait_for_code(f"http://localhost:{port}/callback", "s", timeout=10))
        await asyncio.sleep(0.3)
        async with aiohttp.ClientSession() as s:
            await s.get(f"http://127.0.0.1:{port}/callback?error=access_denied&state=s")
        return await task

    with pytest.raises(PlatformError, match="canceló"):
        run(main())


# --------------------------------------------------------- tokens (sin keyring real)
@pytest.fixture
def fake_store(monkeypatch):
    store = {}
    monkeypatch.setattr(auth.credentials, "get_json", lambda k: store.get(k))
    monkeypatch.setattr(auth.credentials, "set_json", lambda k, v: store.__setitem__(k, dict(v)))
    return store


def test_token_caducado_se_renueva(fake_store, monkeypatch):
    fake_store["kick/account/bot"] = {"access_token": "viejo", "refresh_token": "r1", "expires_at": time.time() - 5,
                                      "user_id": "1", "login": "bot", "display_name": "Bot"}
    calls = []

    async def fake_token_request(form):
        calls.append(form)
        return {"access_token": "nuevo", "refresh_token": "r2", "expires_in": 3600}

    monkeypatch.setattr(auth, "_token_request", fake_token_request)
    store = auth.TokenStore(AccountRole.BOT, "cid", "secret")
    assert run(store.access_token()) == "nuevo"
    assert calls[0]["grant_type"] == "refresh_token" and calls[0]["refresh_token"] == "r1"
    assert fake_store["kick/account/bot"]["refresh_token"] == "r2"
    assert run(store.access_token()) == "nuevo" and len(calls) == 1   # ya no hace falta renovar


def test_sin_cuenta_guardada(fake_store):
    with pytest.raises(auth.NotAuthorizedError):
        auth.TokenStore(AccountRole.STREAMER, "cid", "secret")


def test_call_reintenta_tras_401():
    c, _ = _client_con_streamer()

    class Store:
        refreshed = 0
        async def access_token(self): return f"tok{self.refreshed}"
        async def refresh(self): self.refreshed += 1

    c._tokens = {AccountRole.BOT: Store()}
    seen = []

    async def fn(token):
        seen.append(token)
        if token == "tok0":
            raise KickUnauthorized(401, "x")
        return "ok"

    assert run(c._call(AccountRole.BOT, fn)) == "ok" and seen == ["tok0", "tok1"]


# ------------------------------------------------------------------- otros
def test_normalize_domain():
    assert normalize_domain(" https://Mi-Bot.ngrok-free.app/ ") == "mi-bot.ngrok-free.app"
    assert normalize_domain("mi-bot.ngrok-free.app") == "mi-bot.ngrok-free.app"


def test_connect_all_omite_plataformas_sin_configurar():
    from alpekabot.bot import AlpekaBot
    bot = AlpekaBot(AppConfig(), db_path=":memory:")
    assert run(bot.connect_all()) == ["Configura Twitch o Kick en Configuración para empezar."]
    assert set(bot.platforms) == {Platform.TWITCH, Platform.KICK}


# ------------------------------------------------ integración con una API falsa
def test_flujo_completo_contra_api_falsa(monkeypatch, db):
    """connect → suscripciones → webhook firmado '!hola' → respuesta por la API → disconnect."""
    from conftest import FakeBot

    from alpekabot.features.commands import Command, CommandRepository, CommandService
    from alpekabot.platforms.kick import api as kick_api
    from alpekabot.platforms.kick import client as kick_client

    priv, pem = new_key()
    rec = {"chat": [], "created": [], "deleted": []}
    users = {"Bearer bot-tok": {"user_id": 1, "name": "AlpekaBOT"},
             "Bearer str-tok": {"user_id": 100, "name": "loon_vt"}}

    async def h_users(r): return aiohttp.web.json_response({"data": [users[r.headers["Authorization"]]]})
    async def h_key(r): return aiohttp.web.json_response({"data": {"public_key": pem}})
    async def h_list(r): return aiohttp.web.json_response({"data": []})
    async def h_create(r):
        body = await r.json(); rec["created"].append(body)
        return aiohttp.web.json_response({"data": [{"name": e["name"], "version": 1,
                                                    "subscription_id": "sub-" + e["name"]} for e in body["events"]]})
    async def h_delete(r):
        rec["deleted"].extend(r.query.getall("id")); return aiohttp.web.Response(status=204)
    async def h_channels(r):
        return aiohttp.web.json_response({"data": [{"slug": "loon-vt", "stream_title": "Hola", "category": {"name": "Just Chatting"},
                                                    "stream": {"is_live": True, "start_time": "2025-01-01T10:00:00Z"}}]})
    async def h_chat(r):
        rec["chat"].append((r.headers["Authorization"], await r.json()))
        return aiohttp.web.json_response({"data": {"is_sent": True, "message_id": "out-1"}})

    fake = aiohttp.web.Application()
    fake.router.add_get("/public/v1/users", h_users)
    fake.router.add_get("/public/v1/public-key", h_key)
    fake.router.add_get("/public/v1/events/subscriptions", h_list)
    fake.router.add_post("/public/v1/events/subscriptions", h_create)
    fake.router.add_delete("/public/v1/events/subscriptions", h_delete)
    fake.router.add_get("/public/v1/channels", h_channels)
    fake.router.add_post("/public/v1/chat", h_chat)

    secrets_store = {"kick/client_secret": "sec", "ngrok/authtoken": "ngk"}
    json_store = {
        "kick/account/bot": {"access_token": "bot-tok", "refresh_token": "r", "expires_at": time.time() + 3600,
                             "user_id": "1", "login": "alpekabot", "display_name": "AlpekaBOT"},
        "kick/account/streamer": {"access_token": "str-tok", "refresh_token": "r", "expires_at": time.time() + 3600,
                                  "user_id": "100", "login": "loon_vt", "display_name": "loon_vt",
                                  "scopes": list(auth.SCOPES[AccountRole.STREAMER])},
    }
    monkeypatch.setattr(auth.credentials, "get_secret", lambda k: secrets_store.get(k))
    monkeypatch.setattr(auth.credentials, "get_json", lambda k: json_store.get(k))

    async def main():
        runner = aiohttp.web.AppRunner(fake)
        await runner.setup()
        await aiohttp.web.TCPSite(runner, "127.0.0.1", 0).start()
        monkeypatch.setattr(kick_api, "API_BASE", f"http://127.0.0.1:{runner.addresses[0][1]}/public/v1/")

        bot = FakeBot()
        client = KickClient(KickConfig(client_id="cid", ngrok_domain="mi.ngrok-free.app"), bot.bus)

        async def fake_tunnel(token, domain, port):
            assert token == "ngk" and domain == "mi.ngrok-free.app"
            return f"https://{domain}"
        monkeypatch.setattr(client._tunnel, "start", fake_tunnel)

        bot.platforms[Platform.KICK] = client
        async def send(platform, text): await bot.platforms[platform].send_message(text)
        bot.send_message = send
        repo = CommandRepository(db, bot.bus)
        repo.add(Command(name="hola", response="Hola $user, en $channel jugando $game", cooldown_s=0))
        service = CommandService(bot, repo)
        replies = []
        orig = service.handle
        async def spy(m):
            r = await orig(m); replies.append(r); return r
        service.handle = spy

        await client.connect()
        assert client.is_connected and client.bot.display_name == "AlpekaBOT"
        assert client.stream_info.is_live and client.stream_info.game == "Just Chatting"
        assert {e["name"] for e in rec["created"][0]["events"]} == set(kick_client.EVENTS)

        body = json.dumps(chat_payload(content="!hola", sender={"user_id": 200, "username": "Pepe", "identity": None})).encode()
        ts = now_rfc3339()
        headers = {"Kick-Event-Message-Id": "w1", "Kick-Event-Message-Timestamp": ts,
                   "Kick-Event-Signature": sign(priv, "w1", ts, body), "Kick-Event-Type": "chat.message.sent",
                   "Kick-Event-Version": "1", "Content-Type": "application/json"}
        async with aiohttp.ClientSession() as s:
            async with s.post(f"http://127.0.0.1:{client._webhook.port}{WEBHOOK_PATH}", data=body, headers=headers) as r:
                assert r.status == 200
        for _ in range(50):
            if rec["chat"]:
                break
            await asyncio.sleep(0.05)

        await client.disconnect()
        await runner.cleanup()
        return replies

    replies = run(main())
    assert replies == ["Hola Pepe, en loon_vt jugando Just Chatting"]
    auth_header, sent = rec["chat"][0]
    assert auth_header == "Bearer bot-tok"                       # habla la cuenta del bot…
    assert sent == {"type": "user", "broadcaster_user_id": 100,  # …en el canal del streamer
                    "content": "Hola Pepe, en loon_vt jugando Just Chatting"}
    assert set(rec["deleted"]) == {"sub-" + e for e in kick_client.EVENTS}


def test_navegador_se_abre_fuera_del_hilo_principal(monkeypatch):
    """Regresión: webbrowser.open en el hilo de Qt provocaba 'Cannot enter into task…'."""
    import threading

    from alpekabot.platforms import base

    main_thread = threading.get_ident()
    seen = []
    monkeypatch.setattr(base.webbrowser, "open", lambda url: seen.append((url, threading.get_ident())))
    port = _free_port()

    async def main():
        task = asyncio.ensure_future(auth.wait_for_code(
            f"http://localhost:{port}/callback", "s", timeout=10, open_url="https://id.kick.com/x"))
        for _ in range(50):
            if seen:
                break
            await asyncio.sleep(0.05)
        async with aiohttp.ClientSession() as s:
            await s.get(f"http://127.0.0.1:{port}/callback?code=C&state=s")
        return await task

    assert run(main()) == "C"
    assert seen and seen[0][0] == "https://id.kick.com/x" and seen[0][1] != main_thread
