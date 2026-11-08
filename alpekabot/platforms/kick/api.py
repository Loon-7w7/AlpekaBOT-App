"""Llamadas HTTP a la API pública de Kick (https://docs.kick.com).

Nunca se incluye en los errores el texto de las excepciones de red ni las
cabeceras: llevan el token de acceso.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote

import aiohttp

from ..base import PlatformError

log = logging.getLogger(__name__)

API_BASE = "https://api.kick.com/public/v1/"
ID_BASE = "https://id.kick.com/"
TIMEOUT_S = 10


class KickAPIError(PlatformError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class KickUnauthorized(KickAPIError):
    """401: token caducado o revocado."""


class KickAPI:
    """Sesión HTTP reutilizable. Cada método recibe el token de la cuenta que actúa."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_S))
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def request(
        self, method: str, path: str, token: str | None = None, *,
        params: Any = None, json: Any = None,
    ) -> dict:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        http = await self._http()
        try:
            async with http.request(method, API_BASE + path, headers=headers, params=params, json=json) as r:
                status = r.status
                data = await r.json(content_type=None) if r.content_length != 0 and status != 204 else {}
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            log.warning("Kick API %s %s: fallo de red (%s)", method, path, type(exc).__name__)
            raise KickAPIError(0, "No se pudo contactar con Kick.") from None
        data = data if isinstance(data, dict) else {}
        if status == 401:
            raise KickUnauthorized(401, "Kick rechazó el token (sesión caducada).")
        if status >= 400:
            detail = str(data.get("message", ""))[:150]
            log.warning("Kick API %s %s: HTTP %s %s", method, path, status, detail)
            raise KickAPIError(status, f"Kick devolvió un error ({status}){': ' + detail if detail else ''}")
        return data

    # ------------------------------------------------------------ endpoints
    async def current_user(self, token: str) -> dict:
        data = (await self.request("GET", "users", token)).get("data") or []
        if not data:
            raise KickAPIError(0, "Kick no devolvió los datos de la cuenta.")
        return data[0]

    async def channel(self, token: str, broadcaster_id: str) -> dict | None:
        data = (await self.request("GET", "channels", token, params={"broadcaster_user_id": broadcaster_id})).get("data") or []
        return data[0] if data else None

    async def send_chat(self, token: str, broadcaster_id: str, content: str, reply_to: str | None = None) -> dict:
        body: dict[str, Any] = {"type": "user", "broadcaster_user_id": int(broadcaster_id), "content": content}
        if reply_to:
            body["reply_to_message_id"] = reply_to
        return (await self.request("POST", "chat", token, json=body)).get("data") or {}

    async def channel_by_slug(self, token: str, slug: str) -> dict | None:
        data = (await self.request("GET", "channels", token, params={"slug": slug})).get("data") or []
        return data[0] if data else None

    async def delete_chat_message(self, token: str, message_id: str) -> None:
        await self.request("DELETE", f"chat/{quote(message_id, safe='')}", token)

    async def ban(self, token: str, broadcaster_id: str, user_id: str,
                  duration_min: int | None = None, reason: str = "") -> None:
        """Sin duración = ban permanente; con duración (1–10080 min) = timeout."""
        body: dict[str, Any] = {"broadcaster_user_id": _as_int(broadcaster_id), "user_id": _as_int(user_id)}
        if duration_min is not None:
            body["duration"] = duration_min
        if reason:
            body["reason"] = reason[:100]
        await self.request("POST", "moderation/bans", token, json=body)

    async def unban(self, token: str, broadcaster_id: str, user_id: str) -> None:
        body = {"broadcaster_user_id": _as_int(broadcaster_id), "user_id": _as_int(user_id)}
        await self.request("DELETE", "moderation/bans", token, json=body)

    async def list_subscriptions(self, token: str, broadcaster_id: str) -> list[dict]:
        resp = await self.request("GET", "events/subscriptions", token, params={"broadcaster_user_id": broadcaster_id})
        return resp.get("data") or []

    async def create_subscriptions(self, token: str, events: list[str]) -> list[dict]:
        body = {"events": [{"name": e, "version": 1} for e in events], "method": "webhook"}
        return (await self.request("POST", "events/subscriptions", token, json=body)).get("data") or []

    async def delete_subscriptions(self, token: str, ids: list[str]) -> None:
        if ids:
            await self.request("DELETE", "events/subscriptions", token, params=[("id", i) for i in ids])

    # -------------------------------------------------- premios (fase 7)
    async def list_rewards(self, token: str) -> list[dict]:
        return (await self.request("GET", "channels/rewards", token)).get("data") or []

    async def create_reward(self, token: str, title: str, cost: int, description: str = "") -> dict:
        body: dict[str, Any] = {"title": title[:50], "cost": max(1, int(cost))}
        if description:
            body["description"] = description[:200]
        return (await self.request("POST", "channels/rewards", token, json=body)).get("data") or {}

    async def delete_reward(self, token: str, reward_id: str) -> None:
        await self.request("DELETE", f"channels/rewards/{quote(reward_id, safe='')}", token)

    async def decide_redemptions(self, token: str, ids: list[str], accept: bool) -> list[dict]:
        """Acepta o rechaza canjes (rechazar devuelve los puntos). Devuelve solo los que fallaron."""
        path = "channels/rewards/redemptions/" + ("accept" if accept else "reject")
        data = (await self.request("POST", path, token, json={"ids": ids})).get("data")
        if isinstance(data, dict):  # la documentación no deja claro si viene envuelto
            data = next((v for v in data.values() if isinstance(v, list)), [])
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []

    async def public_key(self) -> str:
        key = ((await self.request("GET", "public-key")).get("data") or {}).get("public_key", "")
        if "BEGIN PUBLIC KEY" not in key:
            raise KickAPIError(0, "Kick no devolvió una clave pública válida.")
        return key


def _as_int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise KickAPIError(0, "ID de usuario de Kick no válido.") from None
