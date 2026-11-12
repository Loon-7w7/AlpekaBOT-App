# Intermediario de Kick (Cloudflare Worker)

Permite que los usuarios de AlpekaBOT conecten Kick **solo iniciando sesión**, con la app de
Kick compartida, sin ngrok ni claves propias. **Solo por white list**: únicamente los canales
de `ALLOWED_CHANNELS` pueden usarlo.

- `POST /oauth/token`: añade el `client_secret` (que nunca llega a la app) y reenvía a Kick.
- `POST /kick/webhook`: es la **Webhook URL** de la app de Kick. Reenvía cada evento a los
  AlpekaBOT conectados a ese canal.
- `GET /ws`: AlpekaBOT se conecta con el token del streamer. El Worker comprueba con Kick
  quién es y si su canal está en la white list.

El cuerpo y la firma de Kick llegan intactos a la app, que sigue verificando la firma RSA.

## Desplegar (una vez)

```powershell
cd relay
npx wrangler login                              # abre el navegador (cuenta de Cloudflare)
npx wrangler deploy                             # muestra la URL: https://alpekabot-kick.<tu-subdominio>.workers.dev
npx wrangler secret put KICK_CLIENT_SECRET      # pega el Client Secret de la app de Kick
npx wrangler secret put ALLOWED_CHANNELS        # p. ej.: loon_vt,amigo1,amigo2
```

Después:

1. En `alpekabot/platforms/kick/relay.py`, pon la URL en `RELAY_BASE` (terminada en `/`).
2. En kick.com → Settings → Developer → tu app: **Webhook URL** =
   `https://alpekabot-kick.<tu-subdominio>.workers.dev/kick/webhook` (con *Enable Webhooks*).
   La Redirect URL sigue siendo `http://localhost:17564/callback`.

## White list

Para añadir o quitar canales, vuelve a ejecutar `npx wrangler secret put ALLOWED_CHANNELS` con la
lista completa, separada por comas. No distingue mayúsculas, y `_` y `-` cuentan igual. Con `*`
quedaría abierto a todos (no recomendado: cualquiera podría gastar la cuota diaria).

## Cuota (plan gratuito)

100 000 peticiones al día; se reinicia a las 00:00 UTC. Cada evento de Kick (mensaje de chat,
follow, sub, canje...) gasta 1 en el Worker y 1 en el Durable Object. Las conexiones abiertas no
gastan gracias a la hibernación. El uso se ve en el panel de Cloudflare → Workers → alpekabot-kick.

## Probar en local

```powershell
npx wrangler dev --var KICK_CLIENT_SECRET:prueba --var ALLOWED_CHANNELS:loon_vt
```
