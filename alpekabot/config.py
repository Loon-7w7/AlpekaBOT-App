"""Configuración NO secreta del bot, guardada en ``data/config.json``.

Los secretos (client secret, tokens) nunca van aquí: ver ``credentials.py``.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields

from .paths import CONFIG_FILE, ensure_dirs

log = logging.getLogger(__name__)


@dataclass
class TwitchConfig:
    # La app de Twitch es compartida (platforms/twitch/auth.py:CLIENT_ID): aquí no hay credenciales.
    # Las claves antiguas client_id / redirect_url de config.json se ignoran al cargar.
    auto_connect: bool = False


@dataclass
class KickConfig:
    # False = app compartida de AlpekaBOT vía el intermediario de Cloudflare (relay/).
    # True = app propia + ngrok (avanzado): usa los campos de abajo.
    use_own_app: bool = False
    client_id: str = ""
    # Debe coincidir EXACTAMENTE con la "Redirect URL" de la app en kick.com → Settings → Developer
    redirect_url: str = "http://localhost:17564/callback"
    # Dominio estático gratuito de ngrok, p. ej. "mi-bot.ngrok-free.app"
    ngrok_domain: str = ""


@dataclass
class SongRequestConfig:
    enabled: bool = True          # peticiones abiertas
    permission: int = 0           # Permission mínimo para !sr (0 = Todos)
    max_duration_min: int = 10
    max_per_user: int = 3         # canciones pendientes por usuario
    volume: int = 50


DEFAULT_LINK_WHITELIST = ("twitch.tv", "clips.twitch.tv", "kick.com", "youtube.com", "youtu.be")


@dataclass
class ModerationConfig:
    enabled: bool = True              # interruptor general de los filtros automáticos
    announce: bool = True             # avisar en el chat de cada sanción automática
    # Escalado por número de infracción: 0 = borrar + aviso, N > 0 = timeout de N s, -1 = ban
    escalation: list[int] = field(default_factory=lambda: [0, 60, 600])
    strike_reset_min: int = 60        # las infracciones se olvidan tras este tiempo sin otra
    links_enabled: bool = True
    links_exempt: int = 2             # Permission mínimo exento (2 = VIP)
    links_whitelist: list[str] = field(default_factory=lambda: list(DEFAULT_LINK_WHITELIST))
    permit_s: int = 60                # !permit: segundos para enviar UN enlace
    words_enabled: bool = True
    words_exempt: int = 3             # 3 = solo moderadores y streamer


@dataclass
class VTubeStudioConfig:
    enabled: bool = False         # conectar a VTube Studio (se reconecta solo)
    host: str = "127.0.0.1"
    port: int = 8001


@dataclass
class TtsConfig:
    enabled: bool = False         # apagado hasta que el streamer lo active: cualquiera podría hablar
    command: str = "tts"          # nombre del comando, sin el prefijo
    permission: int = 0           # Permission mínimo (0 = Todos)
    cooldown_s: int = 10          # cooldown global, como los comandos
    max_chars: int = 200
    max_queue: int = 10
    read_name: bool = False       # «Pepe dice: hola»
    voice: str = "es-MX-DaliaNeural"
    rate: int = 0                 # velocidad en %, de -50 a 50
    volume: int = 100
    output_device: str = ""       # id del dispositivo de audio ("" = predeterminado de Windows)


@dataclass
class AppConfig:
    twitch: TwitchConfig = field(default_factory=TwitchConfig)
    kick: KickConfig = field(default_factory=KickConfig)
    songs: SongRequestConfig = field(default_factory=SongRequestConfig)
    moderation: ModerationConfig = field(default_factory=ModerationConfig)
    vtube: VTubeStudioConfig = field(default_factory=VTubeStudioConfig)
    tts: TtsConfig = field(default_factory=TtsConfig)

    @classmethod
    def load(cls) -> "AppConfig":
        ensure_dirs()
        if not CONFIG_FILE.exists():
            return cls()
        try:
            raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.exception("No se pudo leer %s; se usa la configuración por defecto", CONFIG_FILE)
            return cls()
        return cls(
            twitch=_from_dict(TwitchConfig, raw.get("twitch", {})),
            kick=_from_dict(KickConfig, raw.get("kick", {})),
            songs=_from_dict(SongRequestConfig, raw.get("songs", {})),
            moderation=_from_dict(ModerationConfig, raw.get("moderation", {})),
            vtube=_from_dict(VTubeStudioConfig, raw.get("vtube", {})),
            tts=_from_dict(TtsConfig, raw.get("tts", {})),
        )

    def save(self) -> None:
        ensure_dirs()
        tmp = CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, CONFIG_FILE)  # escritura atómica


def _from_dict(cls, data: dict):
    """Crea el dataclass ignorando claves desconocidas (compatibilidad entre versiones)."""
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})
