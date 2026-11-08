"""Cliente de VTube Studio contra un servidor WebSocket falso que imita su API."""

import asyncio
import json

import pytest
from aiohttp import WSMsgType, web

from alpekabot.config import VTubeStudioConfig
from alpekabot.integrations import vtube_studio as vts
from alpekabot.integrations.vtube_studio import VTSError, VTSState, VTubeStudioClient


class FakeVTS:
    """Responde como VTube Studio y apunta todo lo que le piden."""

    def __init__(self):
        self.tokens = {"tok-valido"}
        self.deny = False
        self.silent = set()
        self.requests = []
        self.sockets = set()
        self.runner = None
        self.port = 0

    async def start(self, port=0):
        app = web.Application()
        app.router.add_get("/", self._ws)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", port)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    async def stop(self):
        for ws in list(self.sockets):
            await ws.close()
        await self.runner.cleanup()

    def seen(self, message_type):
        return [r for r in self.requests if r["messageType"] == message_type]

    def _reply(self, req):
        kind, data = req["messageType"], req.get("data") or {}
        if kind == "AuthenticationTokenRequest":
            if self.deny:
                return "APIError", {"errorID": 50, "message": "User denied"}
            self.tokens.add("tok-nuevo")
            return "AuthenticationTokenResponse", {"authenticationToken": "tok-nuevo"}
        if kind == "AuthenticationRequest":
            return "AuthenticationResponse", {"authenticated": data.get("authenticationToken") in self.tokens}
        if kind == "CurrentModelRequest":
            return "CurrentModelResponse", {"modelName": "Alpeka"}
        if kind == "HotkeysInCurrentModelRequest":
            return "HotkeysInCurrentModelResponse", {"availableHotkeys": [
                {"hotkeyID": "h1", "name": "Saludo", "type": "TriggerAnimation"},
                {"hotkeyID": "h2", "name": "", "type": "ToggleExpression"},
                {"name": "sin id"},
            ]}
        if kind == "ExpressionStateRequest":
            return "ExpressionStateResponse", {"expressions": [
                {"file": "risa.exp3.json", "name": "Risa", "active": True},
                {"file": "", "name": "sin archivo"},
            ]}
        if kind == "HotkeyTriggerRequest" and data.get("hotkeyID") == "no-existe":
            return "APIError", {"errorID": 300, "message": "Hotkey not found"}
        return kind.replace("Request", "Response"), {}

    async def _ws(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.sockets.add(ws)
        try:
            async for msg in ws:
                if msg.type is not WSMsgType.TEXT:
                    continue
                req = json.loads(msg.data)
                self.requests.append(req)
                if req["messageType"] in self.silent:
                    continue
                kind, data = self._reply(req)
                await ws.send_str("no es json")  # el cliente debe ignorar la basura
                await ws.send_json({"apiName": "VTubeStudioPublicAPI", "requestID": req["requestID"],
                                    "messageType": kind, "data": data})
        finally:
            self.sockets.discard(ws)
        return ws


class Env:
    def __init__(self, token=None):
        self.server = FakeVTS()
        self.token = token
        self.statuses = []
        self.client = None

    async def __aenter__(self):
        await self.server.start()
        self.client = VTubeStudioClient(
            VTubeStudioConfig(enabled=True, port=self.server.port), self.statuses.append,
            get_token=lambda: self.token, set_token=lambda v: setattr(self, "token", v),
        )
        return self

    async def __aexit__(self, *exc):
        await self.client.stop()
        await self.server.stop()

    async def until(self, state):
        await wait_for(lambda: self.client.status.state is state)


async def wait_for(check, timeout=3.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not check():
        if loop.time() > end:
            raise AssertionError("no se cumplió a tiempo")
        await asyncio.sleep(0.005)


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------- autenticación
def test_desactivado_no_conecta():
    async def main():
        client = VTubeStudioClient(VTubeStudioConfig(enabled=False), get_token=lambda: None, set_token=lambda v: None)
        client.start()
        await asyncio.sleep(0)
        assert client.status.state is VTSState.OFF and client._task is None
    run(main())


def test_sin_token_pide_autorizar_y_no_abre_ventana_solo():
    async def main():
        async with Env() as e:
            e.client.start()
            await e.until(VTSState.NEEDS_AUTH)
            assert e.server.seen("AuthenticationTokenRequest") == []
    run(main())


def test_autorizar_guarda_el_token_y_conecta():
    async def main():
        async with Env() as e:
            e.client.start()
            await e.until(VTSState.NEEDS_AUTH)
            await e.client.authorize()
            assert e.token == "tok-nuevo"
            assert e.client.is_connected and e.client.status.model == "Alpeka"
            req = e.server.seen("AuthenticationTokenRequest")[0]
            assert req["data"]["pluginName"] == "AlpekaBOT" and req["apiName"] == "VTubeStudioPublicAPI"
            assert e.server.seen("AuthenticationRequest")[0]["data"]["authenticationToken"] == "tok-nuevo"
    run(main())


def test_autorizar_rechazado_lo_dice_claro():
    async def main():
        async with Env() as e:
            e.server.deny = True
            e.client.start()
            await e.until(VTSState.NEEDS_AUTH)
            with pytest.raises(VTSError, match="Rechazaste"):
                await e.client.authorize()
            assert e.token is None and not e.client.is_connected
    run(main())


def test_autorizar_sin_vts_abierto():
    async def main():
        client = VTubeStudioClient(VTubeStudioConfig(enabled=True), get_token=lambda: None, set_token=lambda v: None)
        with pytest.raises(VTSError, match="no está abierto"):
            await client.authorize()
    run(main())


def test_token_guardado_conecta_directo():
    async def main():
        async with Env(token="tok-valido") as e:
            e.client.start()
            await e.until(VTSState.CONNECTED)
            assert e.client.status.model == "Alpeka"
            assert e.server.seen("AuthenticationTokenRequest") == []
    run(main())


def test_token_revocado_se_borra():
    async def main():
        async with Env(token="tok-revocado") as e:
            e.client.start()
            await e.until(VTSState.NEEDS_AUTH)
            assert e.token is None and "retiró" in e.client.status.detail
    run(main())


# ----------------------------------------------------------------- peticiones
def test_hotkeys_y_expresiones():
    async def main():
        async with Env(token="tok-valido") as e:
            e.client.start()
            await e.until(VTSState.CONNECTED)
            hotkeys = await e.client.hotkeys()
            assert [(h.id, h.name) for h in hotkeys] == [("h1", "Saludo"), ("h2", "ToggleExpression")]
            exprs = await e.client.expressions()
            assert [(x.file, x.name, x.active) for x in exprs] == [("risa.exp3.json", "Risa", True)]
    run(main())


def test_disparar_y_expresion_envian_lo_correcto():
    async def main():
        async with Env(token="tok-valido") as e:
            e.client.start()
            await e.until(VTSState.CONNECTED)
            await e.client.trigger_hotkey("h1")
            await e.client.set_expression("risa.exp3.json", True, fade_s=9)
            await e.client.set_expression("risa.exp3.json", False)
            assert e.server.seen("HotkeyTriggerRequest")[0]["data"] == {"hotkeyID": "h1"}
            on, off = e.server.seen("ExpressionActivationRequest")
            assert on["data"] == {"expressionFile": "risa.exp3.json", "active": True, "fadeTime": 2.0}
            assert off["data"] == {"expressionFile": "risa.exp3.json", "active": False}
    run(main())


def test_error_de_la_api_llega_como_vtserror():
    async def main():
        async with Env(token="tok-valido") as e:
            e.client.start()
            await e.until(VTSState.CONNECTED)
            with pytest.raises(VTSError, match="Hotkey not found") as info:
                await e.client.trigger_hotkey("no-existe")
            assert info.value.error_id == 300
    run(main())


def test_sin_autorizar_no_se_puede_disparar():
    async def main():
        async with Env() as e:
            e.client.start()
            await e.until(VTSState.NEEDS_AUTH)
            with pytest.raises(VTSError, match="no está conectado"):
                await e.client.trigger_hotkey("h1")
            assert e.server.seen("HotkeyTriggerRequest") == []
    run(main())


def test_sin_respuesta_da_error_de_tiempo():
    async def main():
        async with Env(token="tok-valido") as e:
            e.client.start()
            await e.until(VTSState.CONNECTED)
            e.server.silent.add("HotkeyTriggerRequest")
            with pytest.raises(VTSError, match="a tiempo"):
                await e.client.request("HotkeyTriggerRequest", {"hotkeyID": "h1"}, timeout=0.05, need_auth=True)
            assert e.client._pending == {}
    run(main())


# ------------------------------------------------------------------ reconexión
def test_vts_cerrado_avisa_y_reintenta(monkeypatch):
    monkeypatch.setattr(vts, "RECONNECT_S", 0.02)

    async def main():
        async with Env(token="tok-valido") as e:
            port = e.server.port
            await e.server.stop()
            e.client.start()
            await wait_for(lambda: "reintento" in e.client.status.detail)
            assert e.client.status.state is VTSState.CONNECTING
            assert str(port) in e.client.status.detail
            await e.server.start(port)  # VTube Studio se abre
            await e.until(VTSState.CONNECTED)
    run(main())


def test_vts_se_cierra_y_vuelve(monkeypatch):
    monkeypatch.setattr(vts, "RECONNECT_S", 0.02)

    async def main():
        async with Env(token="tok-valido") as e:
            e.client.start()
            await e.until(VTSState.CONNECTED)
            port = e.server.port
            await e.server.stop()
            await wait_for(lambda: not e.client.is_connected)
            assert e.client.status.model == ""
            with pytest.raises(VTSError):
                await e.client.trigger_hotkey("h1")
            await e.server.start(port)
            await e.until(VTSState.CONNECTED)
            assert e.client.status.model == "Alpeka"
    run(main())


def test_peticion_en_curso_falla_si_se_cierra():
    async def main():
        async with Env(token="tok-valido") as e:
            e.client.start()
            await e.until(VTSState.CONNECTED)
            e.server.silent.add("HotkeyTriggerRequest")
            pending = asyncio.ensure_future(e.client.trigger_hotkey("h1"))
            await wait_for(lambda: e.server.seen("HotkeyTriggerRequest"))
            for ws in list(e.server.sockets):
                await ws.close()
            with pytest.raises(VTSError, match="perdió"):
                await pending
    run(main())


def test_stop_y_restart():
    async def main():
        async with Env(token="tok-valido") as e:
            e.client.start()
            await e.until(VTSState.CONNECTED)
            await e.client.stop()
            assert e.client.status.state is VTSState.OFF and e.client.status.model == ""
            await e.client.restart()
            await e.until(VTSState.CONNECTED)
            states = [s.state for s in e.statuses]
            assert states.count(VTSState.CONNECTED) == 2 and VTSState.OFF in states
    run(main())


def test_el_token_no_va_a_los_logs(caplog):
    async def main():
        async with Env() as e:
            e.client.start()
            await e.until(VTSState.NEEDS_AUTH)
            await e.client.authorize()
    with caplog.at_level("DEBUG", logger="alpekabot"):
        run(main())
    assert "tok-nuevo" not in caplog.text
