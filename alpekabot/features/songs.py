"""Song requests: cola de peticiones, lista de respaldo, lista negra y control del reproductor.

El reproductor real (YouTube IFrame dentro de la app) vive en la UI e implementa
el protocolo ``Player``; aquí solo está la lógica, para poder testearla.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
import asyncio
from typing import TYPE_CHECKING, Awaitable, Callable, Protocol

from ..config import SongRequestConfig
from ..core.event_bus import EventBus
from ..core.models import ChatMessage, Permission, Platform
from ..storage.database import Database, utc_now_iso
from .youtube import VideoInfo, YouTubeClient, YouTubeError, format_duration, parse_video_id

if TYPE_CHECKING:
    from .commands import CommandService

log = logging.getLogger(__name__)

BLACKLIST_KINDS = {"video": "Vídeo", "channel": "Canal", "word": "Palabra en el título"}
MAX_CONSECUTIVE_ERRORS = 5
QUEUE_PREVIEW = 3

# Códigos de error del reproductor IFrame de YouTube
# (101/150/153 los reintenta el reproductor en la página de YouTube; si llegan aquí, falló)
_EMBED_BLOCKED = {101, 150, 152, 153}  # el autor no permite reproducirlo fuera de YouTube
WATCH_FAILED = 1000  # tampoco se pudo en la página de YouTube (edad, región, inicio de sesión...)
_ERROR_TEXT = {
    2: "enlace no válido",
    5: "error del reproductor",
    100: "el vídeo no existe o es privado",
    101: "el autor no permite reproducirla fuera de YouTube",
    150: "el autor no permite reproducirla fuera de YouTube",
    153: "el autor no permite reproducirla fuera de YouTube",
    WATCH_FAILED: "YouTube no deja reproducirla aquí (edad, región o inicio de sesión)",
}

Notifier = Callable[[Platform, str], Awaitable[None]]


@dataclass(frozen=True)
class Song:
    video_id: str
    title: str
    channel: str = ""
    duration_s: int = 0
    id: int | None = None
    # Solo en la cola de peticiones:
    requested_by: str = ""
    requester_id: str = ""
    platform: Platform | None = None
    playing: bool = False

    @property
    def label(self) -> str:
        return f"{self.title} ({format_duration(self.duration_s)})" if self.duration_s else self.title


@dataclass(frozen=True)
class SongStatus:
    current: Song | None
    from_backup: bool
    paused: bool
    volume: int
    active: bool


@dataclass(frozen=True)
class SongQueued:
    """Aviso para la interfaz: una petición del chat entró en la cola (tarjeta en el chat)."""

    platform: Platform
    song: Song
    position: int          # 0 = suena ya
    playing: bool
    reply: str             # texto que el bot envía al chat (para no mostrarlo dos veces)


@dataclass(frozen=True)
class BlacklistEntry:
    id: int
    kind: str
    value: str


class Player(Protocol):
    # Cada evento incluye el ID del vídeo al que se refiere (para descartar eventos atrasados)
    on_ended: Callable[[str], None] | None
    on_error: Callable[[int, str], None] | None
    on_playing: Callable[[str], None] | None

    def load(self, video_id: str, volume: int) -> None: ...
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def stop(self) -> None: ...
    def set_volume(self, volume: int) -> None: ...


# ------------------------------------------------------------- repositorio
class SongRepository:
    def __init__(self, db: Database, bus: EventBus) -> None:
        self._db = db
        self._bus = bus

    def _changed(self) -> None:
        self._bus.songs_changed.emit()

    @staticmethod
    def _queue_row(r) -> Song:
        return Song(
            id=r["id"], video_id=r["video_id"], title=r["title"], channel=r["channel"],
            duration_s=r["duration_s"], requested_by=r["requested_by"], requester_id=r["requester_id"],
            platform=Platform(r["platform"]), playing=bool(r["playing"]),
        )

    # --- cola
    def queue_list(self) -> list[Song]:
        """Toda la cola: primero la que suena (si hay), luego las pendientes en orden."""
        rows = self._db.conn.execute("SELECT * FROM song_queue ORDER BY playing DESC, position").fetchall()
        return [self._queue_row(r) for r in rows]

    def queue_pending(self) -> list[Song]:
        return [s for s in self.queue_list() if not s.playing]

    def queue_add(self, info: VideoInfo, user_name: str, user_id: str, platform: Platform) -> Song:
        with self._db.conn:
            pos = self._db.conn.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM song_queue").fetchone()[0]
            cur = self._db.conn.execute(
                "INSERT INTO song_queue (video_id, title, channel, duration_s, requested_by, requester_id,"
                " platform, position, playing, created_at) VALUES (?,?,?,?,?,?,?,?,0,?)",
                (info.video_id, info.title, info.channel, info.duration_s, user_name, user_id,
                 platform.value, pos, utc_now_iso()),
            )
        self._changed()
        return Song(info.video_id, info.title, info.channel, info.duration_s, cur.lastrowid,
                    user_name, user_id, platform)

    def queue_remove(self, song_id: int) -> None:
        with self._db.conn:
            self._db.conn.execute("DELETE FROM song_queue WHERE id = ?", (song_id,))
        self._changed()

    def queue_clear_pending(self) -> int:
        with self._db.conn:
            n = self._db.conn.execute("DELETE FROM song_queue WHERE playing = 0").rowcount
        self._changed()
        return n

    def queue_set_playing(self, song_id: int, playing: bool) -> None:
        with self._db.conn:
            self._db.conn.execute("UPDATE song_queue SET playing = ? WHERE id = ?", (int(playing), song_id))
        self._changed()

    def queue_reset_playing(self) -> None:
        with self._db.conn:
            self._db.conn.execute("UPDATE song_queue SET playing = 0")

    def queue_has_video(self, video_id: str) -> bool:
        return self._db.conn.execute("SELECT 1 FROM song_queue WHERE video_id = ?", (video_id,)).fetchone() is not None

    def pending_count(self, platform: Platform, user_id: str) -> int:
        return self._db.conn.execute(
            "SELECT COUNT(*) FROM song_queue WHERE playing = 0 AND platform = ? AND requester_id = ?",
            (platform.value, user_id),
        ).fetchone()[0]

    def last_pending_of(self, platform: Platform, user_id: str) -> Song | None:
        row = self._db.conn.execute(
            "SELECT * FROM song_queue WHERE playing = 0 AND platform = ? AND requester_id = ?"
            " ORDER BY position DESC LIMIT 1", (platform.value, user_id),
        ).fetchone()
        return self._queue_row(row) if row else None

    def queue_move(self, song_id: int, delta: int) -> None:
        self._move("song_queue", self.queue_pending(), song_id, delta)

    # --- respaldo
    def backup_list(self) -> list[Song]:
        rows = self._db.conn.execute("SELECT * FROM song_backup ORDER BY position").fetchall()
        return [Song(id=r["id"], video_id=r["video_id"], title=r["title"], channel=r["channel"],
                     duration_s=r["duration_s"]) for r in rows]

    def backup_add(self, info: VideoInfo) -> Song:
        try:
            with self._db.conn:
                pos = self._db.conn.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM song_backup").fetchone()[0]
                cur = self._db.conn.execute(
                    "INSERT INTO song_backup (video_id, title, channel, duration_s, position, created_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (info.video_id, info.title, info.channel, info.duration_s, pos, utc_now_iso()),
                )
        except sqlite3.IntegrityError:
            raise ValueError("Esa canción ya está en la lista de respaldo.") from None
        self._changed()
        return Song(info.video_id, info.title, info.channel, info.duration_s, cur.lastrowid)

    def backup_remove(self, song_id: int) -> None:
        with self._db.conn:
            self._db.conn.execute("DELETE FROM song_backup WHERE id = ?", (song_id,))
        self._changed()

    def backup_move(self, song_id: int, delta: int) -> None:
        self._move("song_backup", self.backup_list(), song_id, delta)

    # --- lista negra
    def blacklist_list(self) -> list[BlacklistEntry]:
        rows = self._db.conn.execute("SELECT * FROM song_blacklist ORDER BY kind, value").fetchall()
        return [BlacklistEntry(r["id"], r["kind"], r["value"]) for r in rows]

    def blacklist_add(self, kind: str, value: str) -> None:
        if kind not in BLACKLIST_KINDS:
            raise ValueError("Tipo de bloqueo no válido.")
        value = value.strip()
        if kind == "video":
            value = parse_video_id(value) or value
        if not value:
            raise ValueError("Escribe qué quieres bloquear.")
        try:
            with self._db.conn:
                self._db.conn.execute(
                    "INSERT INTO song_blacklist (kind, value, created_at) VALUES (?,?,?)",
                    (kind, value, utc_now_iso()),
                )
        except sqlite3.IntegrityError:
            raise ValueError("Ya está en la lista negra.") from None
        self._changed()

    def blacklist_remove(self, entry_id: int) -> None:
        with self._db.conn:
            self._db.conn.execute("DELETE FROM song_blacklist WHERE id = ?", (entry_id,))
        self._changed()

    def is_blacklisted(self, info: VideoInfo) -> bool:
        title, channel = info.title.casefold(), info.channel.casefold()
        for e in self.blacklist_list():
            v = e.value.casefold()
            if e.kind == "video" and e.value == info.video_id:
                return True
            if e.kind == "channel" and (v == channel or e.value == info.channel_id):
                return True
            if e.kind == "word" and v in title:
                return True
        return False

    # --- utilidades
    def _move(self, table: str, items: list[Song], song_id: int, delta: int) -> None:
        ids = [s.id for s in items]
        if song_id not in ids:
            return
        i = ids.index(song_id)
        j = max(0, min(len(ids) - 1, i + delta))
        if i == j:
            return
        ids.insert(j, ids.pop(i))
        with self._db.conn:
            for pos, sid in enumerate(ids, start=1):
                self._db.conn.execute(f"UPDATE {table} SET position = ? WHERE id = ?", (pos, sid))  # noqa: S608
        self._changed()


# ----------------------------------------------------------------- servicio
class SongService:
    def __init__(
        self,
        bus: EventBus,
        repo: SongRepository,
        config: SongRequestConfig,
        youtube: YouTubeClient,
        save_config: Callable[[], None] = lambda: None,
        now_playing_path: Path | None = None,
        notify: Notifier | None = None,
    ) -> None:
        self._notify = notify
        self._unplayable: set[str] = set()  # vídeos que YouTube no deja reproducir (esta sesión)
        self._bus = bus
        self.repo = repo
        self.config = config
        self.youtube = youtube
        self._save_config = save_config
        self._now_playing_path = now_playing_path
        self._player: Player | None = None
        self._current: Song | None = None
        self._from_backup = False
        self._paused = False
        self._active = False
        self._backup_index = 0
        self._errors = 0
        repo.queue_reset_playing()  # tras un reinicio, la que sonaba vuelve a ser la primera pendiente
        self._write_now_playing()

    # ---------------------------------------------------------- reproductor
    def attach_player(self, player: Player) -> None:
        self._player = player
        player.on_ended = self._on_ended
        player.on_error = self._on_error
        player.on_playing = self._on_playing

    def _is_current(self, video_id: str) -> bool:
        """¿El evento se refiere a la canción actual? (descarta eventos atrasados de la anterior)"""
        return self._current is not None and (not video_id or video_id == self._current.video_id)

    def _on_ended(self, video_id: str = "") -> None:
        if self._is_current(video_id):
            self._advance()

    def _on_playing(self, video_id: str = "") -> None:
        if self._is_current(video_id):
            self._errors = 0

    def _on_error(self, code: int, video_id: str = "") -> None:
        if not self._is_current(video_id):
            log.debug("Error %s de un vídeo que ya no suena (%s): se ignora", code, video_id)
            return
        song = self._current
        reason = _ERROR_TEXT.get(code, f"código {code}")
        log.warning("YouTube no pudo reproducir «%s»: %s; se salta", song.title, reason)
        if code in _EMBED_BLOCKED or code in (100, WATCH_FAILED):
            self._unplayable.add(song.video_id)
        if song.requested_by and song.platform and self._notify:
            text = f"@{song.requested_by} No se pudo reproducir «{song.title[:60]}»: {reason}. Se salta."
            asyncio.ensure_future(self._safe_notify(song.platform, text))
        self._errors += 1
        if self._errors >= MAX_CONSECUTIVE_ERRORS:
            log.error("Demasiados errores seguidos del reproductor: se detiene la música")
            self._errors = 0
            self.stop()
            return
        self._advance()

    async def _safe_notify(self, platform: Platform, text: str) -> None:
        try:
            await self._notify(platform, text)
        except Exception:
            log.info("No se pudo avisar en el chat de %s", platform.label)

    @property
    def status(self) -> SongStatus:
        return SongStatus(self._current, self._from_backup, self._paused, self.config.volume, self._active)

    def _emit(self) -> None:
        self._bus.song_status.emit(self.status)

    def _set_current(self, song: Song | None, from_backup: bool = False) -> None:
        self._current, self._from_backup, self._paused = song, from_backup, False
        if self._player is not None:
            if song is None:
                self._player.stop()
            else:
                self._player.load(song.video_id, self.config.volume)
        if song:
            log.info("🎵 Suena: %s%s", song.title, f" (pedida por {song.requested_by})" if song.requested_by else "")
        self._write_now_playing()
        self._emit()

    def _advance(self) -> None:
        """Termina la canción actual y pasa a la siguiente (cola → respaldo → nada)."""
        if self._current is not None and not self._from_backup:
            self.repo.queue_remove(self._current.id)
        pending = self.repo.queue_pending()
        if pending:
            self._active = True
            nxt = pending[0]
            self.repo.queue_set_playing(nxt.id, True)
            self._set_current(replace(nxt, playing=True))
            return
        backup = self.repo.backup_list() if self._active else []
        if backup:
            song = backup[self._backup_index % len(backup)]
            self._backup_index += 1
            self._set_current(song, from_backup=True)
        else:
            self._set_current(None)

    def _write_now_playing(self) -> None:
        if self._now_playing_path is None:
            return
        s = self._current
        text = "" if s is None else (f"{s.title} — pedida por {s.requested_by}" if s.requested_by else s.title)
        try:
            self._now_playing_path.write_text(text, encoding="utf-8")
        except OSError:
            log.warning("No se pudo escribir %s", self._now_playing_path)

    # ------------------------------------------------------------- controles
    def play(self) -> None:
        self._active = True
        if self._current is None:
            self._advance()
        elif self._paused:
            self._paused = False
            if self._player:
                self._player.resume()
            self._emit()

    def pause(self) -> None:
        if self._current is not None and not self._paused:
            self._paused = True
            if self._player:
                self._player.pause()
            self._emit()

    def skip(self) -> Song | None:
        skipped = self._current
        if skipped is not None:
            self._advance()
        return skipped

    def stop(self) -> None:
        """Detiene la música. Una petición que sonaba vuelve a la cola (primera)."""
        self._active = False
        if self._current is not None and not self._from_backup:
            self.repo.queue_set_playing(self._current.id, False)
        self._set_current(None)

    def set_volume(self, volume: int) -> int:
        volume = max(0, min(100, int(volume)))
        self.config.volume = volume
        self._save_config()
        if self._player:
            self._player.set_volume(volume)
        self._emit()
        return volume

    def set_enabled(self, enabled: bool) -> None:
        self.config.enabled = enabled
        self._save_config()
        self._emit()

    def update_settings(self, permission: Permission, max_duration_min: int, max_per_user: int) -> None:
        self.config.permission = int(permission)
        self.config.max_duration_min = max(0, int(max_duration_min))
        self.config.max_per_user = max(0, int(max_per_user))
        self._save_config()

    async def find_video(self, query: str) -> VideoInfo:
        """Busca por enlace o texto (para añadir a la lista de respaldo desde la app)."""
        vid = parse_video_id(query)
        info = await (self.youtube.get_video(vid) if vid else self.youtube.search(query))
        if info is None:
            raise YouTubeError("No encontré esa canción.")
        if info.is_live:
            raise YouTubeError("No se aceptan directos.")
        return info

    async def add_from_app(self, query: str, user_name: str, platform: Platform) -> Song:
        """Petición hecha desde la app (como el streamer: sin límites de duración ni de cola).

        Lanza YouTubeError con un mensaje para el usuario si no se puede añadir.
        """
        info = await self.find_video(query)
        reason = self._reject_reason(info, is_owner=True)
        if reason:
            raise YouTubeError(reason)
        song = self.repo.queue_add(info, user_name, "app", platform)
        if self._current is None:
            self._advance()
        return song

    def remove(self, song_id: int) -> None:
        if self._current is not None and not self._from_backup and self._current.id == song_id:
            self.skip()
        else:
            self.repo.queue_remove(song_id)

    def remove_backup(self, song_id: int) -> None:
        self.repo.backup_remove(song_id)
        if self._current is not None and self._from_backup and self._current.id == song_id:
            self._advance()

    # -------------------------------------------------------------- peticiones
    def _reject_reason(self, info: VideoInfo, is_owner: bool) -> str | None:
        # Los vídeos «no incrustables» SÍ se aceptan: el reproductor usa la página de YouTube
        if info.video_id in self._unplayable:
            return "Ese vídeo no se pudo reproducir antes (restricción de YouTube). Prueba otra versión, p. ej. «lyrics»."
        if info.is_live:
            return "No se aceptan directos."
        max_s = self.config.max_duration_min * 60
        if not is_owner and max_s and info.duration_s > max_s:
            return f"La canción dura {format_duration(info.duration_s)}; el máximo es {self.config.max_duration_min} min."
        if self.repo.is_blacklisted(info):
            return "Esa canción no está permitida."
        if self.repo.queue_has_video(info.video_id):
            return "Esa canción ya está en la cola."
        return None

    async def request(self, msg: ChatMessage, query: str) -> str:
        at = f"@{msg.user_name}"
        cfg = self.config
        if not cfg.enabled:
            return f"{at} Las peticiones de canciones están cerradas."
        if msg.permission < Permission(cfg.permission):
            return f"{at} Las peticiones son solo para {Permission(cfg.permission).label.lower()}."
        query = query.strip()
        if not query:
            return f"{at} Uso: !sr <enlace de YouTube o nombre de la canción>"
        is_owner = msg.permission >= Permission.BROADCASTER
        if not is_owner and cfg.max_per_user and self.repo.pending_count(msg.platform, msg.user_id) >= cfg.max_per_user:
            return f"{at} Ya tienes {cfg.max_per_user} canciones en la cola. Espera a que suenen."
        try:
            vid = parse_video_id(query)
            info = await (self.youtube.get_video(vid) if vid else self.youtube.search(query))
        except YouTubeError as exc:
            return f"{at} {exc}"
        if info is None:
            return f"{at} No encontré esa canción."
        reason = self._reject_reason(info, is_owner)
        if reason:
            return f"{at} {reason}"
        song = self.repo.queue_add(info, msg.user_name, msg.user_id, msg.platform)
        if self._current is None:
            # Nada sonando: empieza ya. (Si suena el respaldo, la petición va justo después.)
            self._advance()
            reply = f"{at} 🎵 Suena ahora: {song.label}"
            self._bus.song_queued.emit(SongQueued(msg.platform, song, 0, True, reply))
            return reply
        position = [s.id for s in self.repo.queue_pending()].index(song.id) + 1
        reply = f"{at} 🎵 Añadida: {song.label} — posición #{position}"
        self._bus.song_queued.emit(SongQueued(msg.platform, song, position, False, reply))
        return reply

    # ------------------------------------------------------- comandos de chat
    def register_commands(self, commands: "CommandService") -> None:
        mod = Permission.MODERATOR
        commands.register("sr", self._cmd_sr)
        commands.register("song", self._cmd_song)
        commands.register("queue", self._cmd_queue)
        commands.register("wrongsong", self._cmd_wrongsong)
        commands.register("skip", self._cmd_skip, mod)
        commands.register("pause", self._cmd_pause, mod)
        commands.register("play", self._cmd_play, mod)
        commands.register("volume", self._cmd_volume, mod)
        commands.register("clearqueue", self._cmd_clearqueue, mod)

    async def _cmd_sr(self, msg: ChatMessage, args: list[str]) -> str:
        return await self.request(msg, " ".join(args))

    async def _cmd_song(self, msg: ChatMessage, _args: list[str]) -> str:
        s = self._current
        if s is None:
            return f"@{msg.user_name} No está sonando nada."
        who = f"pedida por {s.requested_by}" if s.requested_by else "lista de respaldo"
        return f"🎵 Suena: {s.label} — {who}"

    async def _cmd_queue(self, msg: ChatMessage, _args: list[str]) -> str:
        pending = self.repo.queue_pending()
        if not pending:
            return f"@{msg.user_name} La cola está vacía. ¡Pide una con !sr!"
        items = [f"{i}. {s.title[:40]} ({s.requested_by})" for i, s in enumerate(pending[:QUEUE_PREVIEW], 1)]
        more = f" … y {len(pending) - QUEUE_PREVIEW} más" if len(pending) > QUEUE_PREVIEW else ""
        return "Siguientes: " + " | ".join(items) + more

    async def _cmd_wrongsong(self, msg: ChatMessage, _args: list[str]) -> str:
        song = self.repo.last_pending_of(msg.platform, msg.user_id)
        if song is None:
            return f"@{msg.user_name} No tienes canciones pendientes en la cola."
        self.repo.queue_remove(song.id)
        return f"@{msg.user_name} Quitada de la cola: {song.title}"

    async def _cmd_skip(self, msg: ChatMessage, _args: list[str]) -> str:
        skipped = self.skip()
        return f"⏭ Saltada: {skipped.title}" if skipped else f"@{msg.user_name} No está sonando nada."

    async def _cmd_pause(self, msg: ChatMessage, _args: list[str]) -> str:
        self.pause()
        return "⏸ Música en pausa."

    async def _cmd_play(self, msg: ChatMessage, _args: list[str]) -> str:
        self.play()
        return "▶ Música en marcha." if self._current else f"@{msg.user_name} No hay canciones para reproducir."

    async def _cmd_volume(self, msg: ChatMessage, args: list[str]) -> str:
        if not args:
            return f"🔊 Volumen: {self.config.volume}"
        try:
            value = int(args[0].rstrip("%"))
        except ValueError:
            return f"@{msg.user_name} Uso: !volume 0-100"
        return f"🔊 Volumen: {self.set_volume(value)}"

    async def _cmd_clearqueue(self, msg: ChatMessage, _args: list[str]) -> str:
        n = self.repo.queue_clear_pending()
        return f"🗑 Cola vaciada ({n} canciones)."
