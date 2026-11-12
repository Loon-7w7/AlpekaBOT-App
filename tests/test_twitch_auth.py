"""Login de Twitch con la app pública compartida (Device Code) y renovación sin secreto."""

import asyncio

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from twitchAPI.type import AuthScope, InvalidRefreshTokenException

import twitchAPI.oauth
from alpekabot.platforms.base import PlatformError
from alpekabot.platforms.twitch import auth


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _no_sleep(_s):
    return None


class FakeTwitchId:
    """Imita id.twitch.tv/oauth2/{device,token}."""

    def __init__(self, token_replies):
        self.token_replies = list(token_replies)
        self.requests: list[dict] = []
        app = web.Application()
        app.router.add_post("/oauth2/device", self._device)
        app.router.add_post("/oauth2/token", self._token)
        self.server = TestServer(app)

    @property
    def base(self) -> str:
        return str(self.server.make_url("/oauth2/"))

    async def _device(self, request):
        form = dict(await request.post())
        self.requests.append(form)
        if form.get("client_id") != auth.CLIENT_ID:
            return web.json_response({"status": 400, "message": "invalid client"}, status=400)
        return web.json_response({
            "device_code": "dev123", "user_code": "ABCDEFGH", "expires_in": 1800, "interval": 5,
            "verification_uri": "https://www.twitch.tv/activate?public=true&device-code=ABCDEFGH",
        })

    async def _token(self, request):
        form = dict(await request.post())
        self.requests.append(form)
        status, body = self.token_replies.pop(0)
        return web.json_response(body, status=status)


SCOPES = [AuthScope.USER_READ_CHAT]
PENDING = (400, {"status": 400, "message": "authorization_pending"})


def _flow(fake: FakeTwitchId, **kw):
    async def go():
        await fake.server.start_server()
        try:
            async with aiohttp.ClientSession() as s:
                code = await auth.request_device_code(s, SCOPES, base_url=fake.base)
                return code, await auth.poll_device_token(s, code, SCOPES, base_url=fake.base,
                                                          sleep=kw.get("sleep", _no_sleep))
        finally:
            await fake.server.close()
    return run(go())


def test_device_code_espera_y_devuelve_tokens_sin_secreto():
    fake = FakeTwitchId([PENDING, PENDING, (200, {"access_token": "acc", "refresh_token": "ref"})])
    code, tokens = _flow(fake)
    assert code.user_code == "ABCDEFGH" and code.interval == 5
    assert tokens == ("acc", "ref")
    token_reqs = fake.requests[1:]
    assert len(token_reqs) == 3
    assert all("client_secret" not in r for r in fake.requests)
    assert token_reqs[0]["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
    assert token_reqs[0]["device_code"] == "dev123"
    assert token_reqs[0]["client_id"] == auth.CLIENT_ID


def test_device_code_respeta_el_intervalo_y_slow_down():
    waits = []

    async def sleep(s):
        waits.append(s)

    fake = FakeTwitchId([(400, {"status": 400, "message": "slow_down"}), PENDING,
                         (200, {"access_token": "a", "refresh_token": "r"})])
    _flow(fake, sleep=sleep)
    assert waits == [5, 10, 10]


def test_device_code_caducado_es_timeout():
    fake = FakeTwitchId([(400, {"status": 400, "message": "invalid device code"})])
    with pytest.raises(asyncio.TimeoutError):
        _flow(fake)


def test_device_code_denegado():
    fake = FakeTwitchId([(400, {"status": 400, "message": "access_denied"})])
    with pytest.raises(PlatformError, match="cancel"):
        _flow(fake)


def test_device_code_error_desconocido():
    fake = FakeTwitchId([(500, {"status": 500, "message": "boom"})])
    with pytest.raises(PlatformError):
        _flow(fake)


# --------------------------------------------------------- renovación
def _refresh(fake: FakeTwitchId, secret=None):
    async def go():
        await fake.server.start_server()
        try:
            return await auth._refresh_public("old", auth.CLIENT_ID, secret, auth_base_url=fake.base)
        finally:
            await fake.server.close()
    return run(go())


def test_twitchapi_usa_la_renovacion_sin_secreto():
    assert twitchAPI.oauth.refresh_access_token is auth._refresh_public


def test_renovacion_sin_secreto_no_envia_client_secret():
    fake = FakeTwitchId([(200, {"access_token": "new", "refresh_token": "new_ref"})])
    assert _refresh(fake) == ("new", "new_ref")
    assert fake.requests[0] == {"refresh_token": "old", "client_id": auth.CLIENT_ID,
                                "grant_type": "refresh_token"}


def test_renovacion_con_secreto_lo_envia():
    fake = FakeTwitchId([(200, {"access_token": "n", "refresh_token": "r"})])
    _refresh(fake, secret="s3")
    assert fake.requests[0]["client_secret"] == "s3"


def test_renovacion_invalida():
    fake = FakeTwitchId([(400, {"status": 400, "message": "Invalid refresh token"})])
    with pytest.raises(InvalidRefreshTokenException):
        _refresh(fake)
