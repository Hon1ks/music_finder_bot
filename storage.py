"""SQLite: кэш отправленных треков (file_id), пользователи с доступом, события для /stats."""
import os
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(os.getenv("DB_PATH", "data/bot.db"))

_db: sqlite3.Connection | None = None


def db() -> sqlite3.Connection:
    global _db
    if _db is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _db = sqlite3.connect(DB_PATH)
        _db.executescript("""
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY, file_id TEXT NOT NULL,
                performer TEXT, title TEXT, duration INTEGER, source TEXT, created INTEGER);
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY, name TEXT, status TEXT NOT NULL, created INTEGER);
            CREATE TABLE IF NOT EXISTS events (
                ts INTEGER, user_id INTEGER, kind TEXT, source TEXT, ok INTEGER, detail TEXT);
            CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
        """)
    return _db


# ---------- кэш: трек, однажды отправленный в Telegram, дальше пересылается по file_id ----------

def cache_get(key: str) -> dict | None:
    row = db().execute("SELECT file_id, performer, title, duration, source FROM cache WHERE key = ?",
                       (key,)).fetchone()
    return dict(zip(("file_id", "performer", "title", "duration", "source"), row)) if row else None


def cache_put(key: str, file_id: str, performer: str, title: str, duration: int | None, source: str) -> None:
    db().execute("INSERT OR REPLACE INTO cache VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (key, file_id, performer, title, duration, source, int(time.time())))
    db().commit()


# ---------- пользователи: allowed / pending / blocked ----------

def user_status(user_id: int) -> str | None:
    row = db().execute("SELECT status FROM users WHERE id = ?", (user_id,)).fetchone()
    return row[0] if row else None


def user_set(user_id: int, status: str, name: str | None = None) -> None:
    db().execute(
        "INSERT INTO users VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET status = excluded.status, "
        "name = COALESCE(excluded.name, users.name)",
        (user_id, name, status, int(time.time())))
    db().commit()


def users_list() -> list[tuple[int, str, str]]:
    return db().execute("SELECT id, COALESCE(name, ''), status FROM users ORDER BY status, created").fetchall()


# ---------- события ----------

def event(user_id: int, kind: str, source: str = "", ok: bool = True, detail: str = "") -> None:
    db().execute("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
                 (int(time.time()), user_id, kind, source, int(ok), detail[:300]))
    db().commit()


def stats(since: int) -> dict:
    q = db().execute
    return {
        "users": q("SELECT COUNT(DISTINCT user_id) FROM events WHERE ts >= ?", (since,)).fetchone()[0],
        "searches": q("SELECT COUNT(*), SUM(ok) FROM events WHERE ts >= ? AND kind = 'search'",
                      (since,)).fetchone(),
        "downloads": q("SELECT source, COUNT(*), SUM(ok) FROM events WHERE ts >= ? AND kind = 'download' "
                       "GROUP BY source ORDER BY 2 DESC", (since,)).fetchall(),
        "cache_hits": q("SELECT COUNT(*) FROM events WHERE ts >= ? AND kind = 'cache'", (since,)).fetchone()[0],
        "yt_blocks": q("SELECT COUNT(*) FROM events WHERE ts >= ? AND kind = 'yt_block'", (since,)).fetchone()[0],
        "cached_total": q("SELECT COUNT(*) FROM cache").fetchone()[0],
    }
