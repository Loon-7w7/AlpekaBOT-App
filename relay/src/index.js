// AlpekaBOT — intermediario de Kick (Cloudflare Worker + Durable Objects).
//
// Por qué existe: Kick exige el client_secret para dar y renovar tokens, y solo
// admite UNA Webhook URL por app. Este Worker guarda el secreto (wrangler secret)
// y reparte los webhooks de la app compartida a cada AlpekaBOT por WebSocket.
//
// Rutas:
//   POST /oauth/token   → id.kick.com/oauth/token añadiendo client_id y client_secret
//   POST /kick/webhook  → Webhook URL de la app de Kick; reenvía al canal correspondiente
//   GET  /ws            → AlpekaBOT se conecta con el token del streamer (Authorization: Bearer)
//
// Seguridad:
//   - Solo canales en ALLOWED_CHANNELS (secreto: "canal1,canal2", o "*" para todos).
//   - El cuerpo y las cabeceras de firma de Kick llegan INTACTOS a la app, que verifica
//     la firma RSA con la clave pública de Kick: este Worker no puede inventar eventos.
//   - Una conexión solo recibe los eventos de SU canal (se comprueba el token con Kick).
//   - Hibernación de WebSockets: una conexión inactiva no gasta cuota.

import { DurableObject } from "cloudflare:workers";

const KICK_HEADERS = [
  "kick-event-message-id",
  "kick-event-message-timestamp",
  "kick-event-signature",
  "kick-event-type",
  "kick-event-version",
  "kick-event-subscription-id",
];
const MAX_BODY = 256 * 1024;
const TOKEN_FIELDS = ["grant_type", "code", "code_verifier", "redirect_uri", "refresh_token"];

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function normalize(name) {
  return String(name || "").trim().toLowerCase().replace(/_/g, "-");
}

function isAllowed(env, ...names) {
  const list = String(env.ALLOWED_CHANNELS || "")
    .split(",")
    .map(normalize)
    .filter(Boolean);
  if (list.includes("*")) return true;
  return names.some((n) => n && list.includes(normalize(n)));
}

function channelStub(env, userId) {
  return env.CHANNELS.get(env.CHANNELS.idFromName(String(userId)));
}

// ------------------------------------------------------------------ tokens
async function token(request, env) {
  let form;
  try {
    form = await request.formData();
  } catch {
    return json({ error: "invalid_request" }, 400);
  }
  const grant = form.get("grant_type");
  if (grant !== "authorization_code" && grant !== "refresh_token") {
    return json({ error: "unsupported_grant_type" }, 400);
  }
  const out = new URLSearchParams();
  for (const key of TOKEN_FIELDS) {
    const value = form.get(key);
    if (value) out.set(key, value);
  }
  out.set("client_id", env.KICK_CLIENT_ID);
  out.set("client_secret", env.KICK_CLIENT_SECRET);
  const resp = await fetch(env.KICK_ID_BASE + "oauth/token", {
    method: "POST",
    body: out,
    headers: {
      "content-type": "application/x-www-form-urlencoded",
      accept: "application/json",
    },
  });
  return new Response(resp.body, {
    status: resp.status,
    headers: { "content-type": resp.headers.get("content-type") || "application/json" },
  });
}

// ----------------------------------------------------------------- webhook
async function webhook(request, env) {
  if (Number(request.headers.get("content-length") || 0) > MAX_BODY) {
    return new Response("demasiado grande", { status: 413 });
  }
  const body = await request.text();
  if (body.length > MAX_BODY) return new Response("demasiado grande", { status: 413 });
  let payload;
  try {
    payload = JSON.parse(body);
  } catch {
    return new Response("json", { status: 400 });
  }
  const b = (payload && payload.broadcaster) || {};
  if (b.user_id === undefined || b.user_id === null) {
    return new Response("sin canal", { status: 400 });
  }
  // Filtro barato ANTES de despertar al Durable Object (la app vuelve a comprobar todo)
  if (!isAllowed(env, b.username, b.channel_slug)) {
    return new Response("canal no autorizado", { status: 403 });
  }
  const headers = {};
  for (const name of KICK_HEADERS) {
    const value = request.headers.get(name);
    if (value !== null) headers[name] = value;
  }
  const listeners = await channelStub(env, b.user_id).broadcast(
    JSON.stringify({ type: "event", headers, body }),
  );
  // Sin AlpekaBOT conectado nadie recibiría el evento: 410 para que Kick lo sepa
  return listeners ? new Response("ok") : new Response("sin oyentes", { status: 410 });
}

// --------------------------------------------------------------- websocket
async function connect(request, env) {
  if (request.headers.get("upgrade") !== "websocket") {
    return new Response("se esperaba un WebSocket", { status: 426 });
  }
  const auth = request.headers.get("authorization") || "";
  if (!auth.startsWith("Bearer ")) return json({ error: "token" }, 401);
  const resp = await fetch(env.KICK_API_BASE + "users", {
    headers: { authorization: auth, accept: "application/json" },
  });
  if (resp.status === 401) return json({ error: "token" }, 401);
  if (!resp.ok) return json({ error: "kick" }, 502);
  let user;
  try {
    user = ((await resp.json()).data || [])[0];
  } catch {
    user = undefined;
  }
  if (!user || user.user_id === undefined) return json({ error: "kick" }, 502);
  if (!isAllowed(env, user.name)) {
    return json({ error: "not_allowed", channel: user.name }, 403);
  }
  return channelStub(env, user.user_id).fetch(request);
}

export default {
  async fetch(request, env) {
    const { pathname } = new URL(request.url);
    if (pathname === "/oauth/token" && request.method === "POST") return token(request, env);
    if (pathname === "/kick/webhook" && request.method === "POST") return webhook(request, env);
    if (pathname === "/ws" && request.method === "GET") return connect(request, env);
    if (pathname === "/" && request.method === "GET") return new Response("AlpekaBOT · Kick");
    return new Response("no existe", { status: 404 });
  },
};

// Un objeto por canal de Kick: guarda las conexiones de los AlpekaBOT de ese canal.
export class KickChannel extends DurableObject {
  async fetch(request) {
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    this.ctx.acceptWebSocket(server); // API de hibernación: sin coste mientras no pasa nada
    return new Response(null, { status: 101, webSocket: client });
  }

  async broadcast(message) {
    let sent = 0;
    for (const ws of this.ctx.getWebSockets()) {
      try {
        ws.send(message);
        sent += 1;
      } catch {
        // conexión ya cerrada: la limpia el runtime
      }
    }
    return sent;
  }

  async webSocketMessage() {
    // La app no envía nada (los pings de protocolo los contesta el runtime)
  }

  async webSocketClose(ws, code, reason) {
    try {
      ws.close(code, reason);
    } catch {
      // ya estaba cerrada
    }
  }
}
