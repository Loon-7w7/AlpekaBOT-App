"""Base de datos SQLite (``data/alpekabot.db``) con migraciones por versión.

Para cambiar el esquema: añadir un script NUEVO al final de ``_MIGRATIONS``.
Nunca editar uno ya publicado.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_MIGRATIONS: list[str] = [
    # v1 — fase 2: comandos y timers
    """
    CREATE TABLE commands (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
        response    TEXT    NOT NULL,
        permission  INTEGER NOT NULL DEFAULT 0,
        cooldown_s  INTEGER NOT NULL DEFAULT 5,
        platforms   TEXT    NOT NULL DEFAULT 'kick,twitch',
        enabled     INTEGER NOT NULL DEFAULT 1,
        use_count   INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT    NOT NULL,
        updated_at  TEXT    NOT NULL
    );
    CREATE TABLE timers (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        name         TEXT    NOT NULL UNIQUE COLLATE NOCASE,
        message      TEXT    NOT NULL,
        interval_min INTEGER NOT NULL DEFAULT 15,
        min_lines    INTEGER NOT NULL DEFAULT 5,
        platforms    TEXT    NOT NULL DEFAULT 'kick,twitch',
        enabled      INTEGER NOT NULL DEFAULT 1,
        created_at   TEXT    NOT NULL,
        updated_at   TEXT    NOT NULL
    );
    """,
    # v2 — fase 3: song requests
    """
    CREATE TABLE song_queue (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        video_id      TEXT    NOT NULL,
        title         TEXT    NOT NULL,
        channel       TEXT    NOT NULL DEFAULT '',
        duration_s    INTEGER NOT NULL DEFAULT 0,
        requested_by  TEXT    NOT NULL,
        requester_id  TEXT    NOT NULL,
        platform      TEXT    NOT NULL,
        position      INTEGER NOT NULL,
        playing       INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT    NOT NULL
    );
    CREATE TABLE song_backup (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        video_id    TEXT    NOT NULL UNIQUE,
        title       TEXT    NOT NULL,
        channel     TEXT    NOT NULL DEFAULT '',
        duration_s  INTEGER NOT NULL DEFAULT 0,
        position    INTEGER NOT NULL,
        created_at  TEXT    NOT NULL
    );
    CREATE TABLE song_blacklist (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        kind        TEXT    NOT NULL CHECK (kind IN ('video', 'channel', 'word')),
        value       TEXT    NOT NULL COLLATE NOCASE,
        created_at  TEXT    NOT NULL,
        UNIQUE (kind, value)
    );
    """,
    # v3 — fase 5: moderación
    """
    CREATE TABLE banned_words (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        pattern     TEXT    NOT NULL,
        is_regex    INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT    NOT NULL,
        UNIQUE (pattern, is_regex)
    );
    CREATE TABLE mod_actions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at  TEXT    NOT NULL,
        platform    TEXT    NOT NULL,
        user_id     TEXT    NOT NULL,
        user_name   TEXT    NOT NULL,
        action      TEXT    NOT NULL CHECK (action IN ('warn', 'delete', 'timeout', 'ban', 'unban', 'permit')),
        duration_s  INTEGER NOT NULL DEFAULT 0,
        reason      TEXT    NOT NULL DEFAULT '',
        source      TEXT    NOT NULL CHECK (source IN ('auto', 'manual')),
        moderator   TEXT    NOT NULL DEFAULT '',
        message     TEXT    NOT NULL DEFAULT ''
    );
    CREATE INDEX mod_actions_created ON mod_actions (created_at);
    """,
    # v4 — fase 7: reacciones (VTube Studio) a canjes y eventos + premios creados por la app
    """
    CREATE TABLE reactions (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        name          TEXT    NOT NULL UNIQUE COLLATE NOCASE,
        trigger       TEXT    NOT NULL,
        reward_id     TEXT    NOT NULL DEFAULT '',
        reward_title  TEXT    NOT NULL DEFAULT '',
        min_amount    INTEGER NOT NULL DEFAULT 0,
        platforms     TEXT    NOT NULL DEFAULT 'kick,twitch',
        steps         TEXT    NOT NULL DEFAULT '[]',
        message       TEXT    NOT NULL DEFAULT '',
        cooldown_s    INTEGER NOT NULL DEFAULT 0,
        enabled       INTEGER NOT NULL DEFAULT 1,
        created_at    TEXT    NOT NULL,
        updated_at    TEXT    NOT NULL
    );
    CREATE TABLE managed_rewards (
        platform    TEXT NOT NULL,
        reward_id   TEXT NOT NULL,
        title       TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL,
        PRIMARY KEY (platform, reward_id)
    );
    """,
    # v5 — una reacción de canje puede tener un premio por plataforma
    """
    ALTER TABLE reactions ADD COLUMN rewards TEXT NOT NULL DEFAULT '[]';
    UPDATE reactions
       SET rewards = json_array(json_object('platform', platforms, 'id', reward_id, 'title', reward_title))
     WHERE trigger = 'reward' AND reward_id != '';
    ALTER TABLE reactions DROP COLUMN reward_id;
    ALTER TABLE reactions DROP COLUMN reward_title;
    """,
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if str(path) != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def _migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for target in range(version, len(_MIGRATIONS)):
            log.info("Migrando base de datos a v%d", target + 1)
            with self.conn:
                self.conn.executescript(_MIGRATIONS[target])
                self.conn.execute(f"PRAGMA user_version = {target + 1}")

    def close(self) -> None:
        self.conn.close()
