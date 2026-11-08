# AlpekaBOT

Bot de chat de escritorio para **Twitch** y **Kick**, para Windows 10/11: comandos, timers, peticiones de canciones de YouTube, moderación, texto a voz y reacciones con VTube Studio.

## Instalación

1. Descarga `AlpekaBOT-Setup-x.y.z.exe` de la sección **Releases**.
2. Ejecútalo. No necesita permisos de administrador.
3. El instalador no tiene certificado de código, así que Windows puede mostrar «Windows protegió tu PC». Pulsa **Más información → Ejecutar de todas formas**.

Al abrirse, la app comprueba si hay una versión nueva y **siempre pregunta** antes de instalarla. También puedes buscarla en **Ajustes → Actualizaciones**. Cada actualización va firmada y se verifica antes de ejecutarse.

## Qué necesitas

Cada persona usa sus propias cuentas y claves; los pasos detallados y los enlaces están en **Ajustes**.

| Función | Necesitas |
|---|---|
| Twitch | Una app en [dev.twitch.tv](https://dev.twitch.tv/console/apps) (Redirect URL `http://localhost:17563`), la cuenta del bot y la tuya |
| Kick | Una app en [kick.com](https://kick.com/settings/developer) (Redirect URL `http://localhost:17564/callback`), cuenta de [ngrok](https://dashboard.ngrok.com/signup) con su dominio estático gratuito, la cuenta del bot y la tuya |
| Canciones | Una clave gratuita de la YouTube Data API v3 |
| Texto a voz | Conexión a internet. Usa el servicio de voces de Microsoft Edge, que no es oficial y podría cambiar |
| VTube Studio | La app abierta en el mismo PC con su API activada |

Haz moderador a la cuenta del bot en tu canal: es necesaria para moderar en Twitch.

## Funciones

- **Comandos** con permisos, cooldown y variables: `$user $target $args $channel $count $platform $uptime $game $title $random(1,100) $pick(a|b|c) $readapi(https://…)`. Los mods también pueden usar `!addcmd`, `!editcmd` y `!delcmd`.
- **Timers**: mensajes automáticos, solo en directo y si ha habido actividad en el chat.
- **Canciones**: `!sr <enlace o nombre>`, `!song`, `!queue`, `!wrongsong`. Para mods: `!skip`, `!pause`, `!play`, `!volume 0-100`, `!clearqueue`.
- **Moderación**: filtros de enlaces y palabras prohibidas con sanciones progresivas, `!timeout`, `!ban`, `!unban`, `!permit` y clic derecho sobre un mensaje.
- **Texto a voz**: comando renombrable (por defecto `!tts`) y `!skiptts` para mods. Empieza desactivado.
- **VTube Studio**: reacciones a canjes, follows, subs, bits y raids.

## Dónde se guardan tus datos

| Qué | Dónde |
|---|---|
| Secretos y tokens | Administrador de credenciales de Windows (entradas `AlpekaBOT/...`) |
| Configuración, comandos, timers y logs | `%LOCALAPPDATA%\AlpekaBOT` |
| Canción actual, para una fuente de texto de OBS | `%LOCALAPPDATA%\AlpekaBOT\now_playing.txt` |

Actualizar o desinstalar no borra estos datos.

## Ejecutar desde el código

Requiere Python 3.13. Así, los datos se guardan en `data/`.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe main.py
$env:QT_QPA_PLATFORM="offscreen"; .\.venv\Scripts\python.exe -m pytest -q tests
```

## Licencia

© Loon. Todos los derechos reservados: consulta [LICENSE](LICENSE). Las librerías de terceros conservan sus licencias, recogidas en `THIRD_PARTY_NOTICES.txt` dentro de la carpeta de la app.
