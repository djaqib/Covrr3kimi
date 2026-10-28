"""Persistent database layer for the Telegram video bot.

Uses Supabase/PostgreSQL when DATABASE_URL is configured, otherwise SQLite.
PostgreSQL connection errors are intentionally fatal instead of silently falling
back to temporary local storage on Render.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    POSTGRES_AVAILABLE = True
except ImportError:
    psycopg2 = None
    RealDictCursor = None
    POSTGRES_AVAILABLE = False


class Database:
    def __init__(self) -> None:
        self.database_url = os.getenv("DATABASE_URL", "").strip()
        self.use_postgres = bool(self.database_url)
        self.lock = threading.RLock()
        self.sqlite_conn = None
        if self.use_postgres:
            if not POSTGRES_AVAILABLE:
                raise RuntimeError("DATABASE_URL is set, but psycopg2-binary is not installed.")
            self._init_postgres()
        else:
            path = os.getenv("SQLITE_PATH", "bot_database.db")
            self.sqlite_conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
            self.sqlite_conn.row_factory = sqlite3.Row
            self.sqlite_conn.execute("PRAGMA journal_mode=WAL")
            self.sqlite_conn.execute("PRAGMA busy_timeout=30000")
        self._create_tables()
        logger.info("Database initialized (%s)", "PostgreSQL/Supabase" if self.use_postgres else "SQLite")

    def _init_postgres(self) -> None:
        url = self.database_url
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        # Supabase requires SSL in most hosted configurations.
        if "sslmode=" not in url:
            url += "&sslmode=require" if "?" in url else "?sslmode=require"
        try:
            self.pg_conn = psycopg2.connect(url, connect_timeout=10, application_name="telegram-video-bot")
            self.pg_conn.autocommit = False
        except Exception as exc:
            raise RuntimeError(f"Could not connect to DATABASE_URL. Check the Supabase connection string and network settings ({type(exc).__name__}).") from exc

    @contextmanager
    def get_connection(self):
        if self.use_postgres:
            with self.lock:
                try:
                    yield self.pg_conn
                    self.pg_conn.commit()
                except Exception:
                    self.pg_conn.rollback()
                    raise
        else:
            with self.lock:
                try:
                    yield self.sqlite_conn
                    self.sqlite_conn.commit()
                except Exception:
                    self.sqlite_conn.rollback()
                    raise

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.use_postgres else sql

    def _create_tables(self) -> None:
        id_type = "BIGSERIAL PRIMARY KEY" if self.use_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
        bool_type = "BOOLEAN" if self.use_postgres else "INTEGER"
        tables = [
            f"""CREATE TABLE IF NOT EXISTS videos (
                id {id_type}, file_id TEXT NOT NULL, file_unique_id TEXT NOT NULL UNIQUE,
                checksum TEXT, file_size BIGINT DEFAULT 0, duration INTEGER DEFAULT 0,
                width INTEGER DEFAULT 0, height INTEGER DEFAULT 0, chat_id BIGINT,
                user_id BIGINT, status TEXT DEFAULT 'queued', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                sent_at TIMESTAMP)""",
            f"""CREATE TABLE IF NOT EXISTS photos (
                id {id_type}, file_id TEXT NOT NULL, file_unique_id TEXT NOT NULL UNIQUE,
                file_size BIGINT DEFAULT 0, chat_id BIGINT, user_id BIGINT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
            f"""CREATE TABLE IF NOT EXISTS queue_items (
                id {id_type}, chat_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
                file_id TEXT NOT NULL, file_unique_id TEXT NOT NULL UNIQUE,
                file_size BIGINT DEFAULT 0, duration INTEGER DEFAULT 0, width INTEGER DEFAULT 0,
                height INTEGER DEFAULT 0, checksum TEXT, added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
            "CREATE INDEX IF NOT EXISTS idx_queue_owner ON queue_items(chat_id, user_id, id)",
            "CREATE INDEX IF NOT EXISTS idx_videos_user ON videos(user_id)",
            f"CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
            f"CREATE TABLE IF NOT EXISTS statistics (metric TEXT PRIMARY KEY, value BIGINT NOT NULL DEFAULT 0, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
            "CREATE TABLE IF NOT EXISTS favorites (user_id BIGINT NOT NULL, file_unique_id TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(user_id, file_unique_id))",
            f"CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT, is_admin {bool_type} DEFAULT FALSE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        ]
        with self.get_connection() as conn:
            cur = conn.cursor()
            for statement in tables:
                cur.execute(statement)
            cur.close()

    def _cursor(self, conn):
        return conn.cursor(cursor_factory=RealDictCursor) if self.use_postgres else conn.cursor()

    def _dict(self, row) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        return dict(row) if not isinstance(row, dict) else row

    # Admin/user methods
    def add_admin(self, user_id: int, username=None, first_name=None, last_name=None) -> bool:
        with self.get_connection() as conn:
            cur = conn.cursor()
            if self.use_postgres:
                cur.execute("INSERT INTO users(user_id,username,first_name,last_name,is_admin) VALUES(%s,%s,%s,%s,TRUE) ON CONFLICT(user_id) DO UPDATE SET username=EXCLUDED.username, first_name=EXCLUDED.first_name, last_name=EXCLUDED.last_name, is_admin=TRUE", (user_id, username, first_name, last_name))
            else:
                cur.execute("INSERT INTO users(user_id,username,first_name,last_name,is_admin) VALUES(?,?,?,?,1) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name, last_name=excluded.last_name, is_admin=1", (user_id, username, first_name, last_name))
            cur.close()
        return True

    def is_admin(self, user_id: int) -> bool:
        with self.get_connection() as conn:
            cur = self._cursor(conn)
            cur.execute(self._sql("SELECT is_admin FROM users WHERE user_id=?"), (user_id,))
            row = cur.fetchone()
            cur.close()
        if not row:
            return False
        val = row["is_admin"] if isinstance(row, dict) else row[0]
        return bool(val)

    def touch_user(self, user_id: int, username=None, first_name=None, last_name=None) -> None:
        with self.get_connection() as conn:
            cur = conn.cursor()
            if self.use_postgres:
                cur.execute("INSERT INTO users(user_id,username,first_name,last_name) VALUES(%s,%s,%s,%s) ON CONFLICT(user_id) DO UPDATE SET username=EXCLUDED.username, first_name=EXCLUDED.first_name, last_name=EXCLUDED.last_name, last_seen=CURRENT_TIMESTAMP", (user_id, username, first_name, last_name))
            else:
                cur.execute("INSERT INTO users(user_id,username,first_name,last_name) VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name, last_name=excluded.last_name, last_seen=CURRENT_TIMESTAMP", (user_id, username, first_name, last_name))
            cur.close()

    # Settings are JSON encoded to preserve bool/int/string types.
    def get_all_settings(self) -> Dict[str, Any]:
        with self.get_connection() as conn:
            cur = self._cursor(conn); cur.execute("SELECT key,value FROM settings"); rows = cur.fetchall(); cur.close()
        result = {}
        for row in rows:
            key, value = (row["key"], row["value"]) if isinstance(row, dict) else (row[0], row[1])
            try: result[key] = json.loads(value)
            except Exception: result[key] = value
        return result

    def set_setting(self, key: str, value: Any) -> None:
        raw = json.dumps(value)
        with self.get_connection() as conn:
            cur = conn.cursor()
            if self.use_postgres:
                cur.execute("INSERT INTO settings(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value, updated_at=CURRENT_TIMESTAMP", (key, raw))
            else:
                cur.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP", (key, raw))
            cur.close()

    # Video library and queue
    def video_exists(self, file_unique_id: str) -> bool:
        with self.get_connection() as conn:
            cur = conn.cursor(); cur.execute(self._sql("SELECT 1 FROM videos WHERE file_unique_id=? LIMIT 1"), (file_unique_id,)); found = cur.fetchone() is not None; cur.close()
        return found

    def add_video(self, file_id: str, file_unique_id: str, checksum: str, file_size: int, duration: int, width: int, height: int, chat_id: int, user_id: int) -> bool:
        with self.get_connection() as conn:
            cur = conn.cursor()
            try:
                cur.execute(self._sql("INSERT INTO videos(file_id,file_unique_id,checksum,file_size,duration,width,height,chat_id,user_id,status) VALUES(?,?,?,?,?,?,?,?,?,'queued')"), (file_id,file_unique_id,checksum,file_size,duration,width,height,chat_id,user_id))
            except Exception as exc:
                # Duplicate unique IDs are expected sometimes; re-raise other database errors.
                if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
                    conn.rollback(); cur.close(); return False
                raise
            cur.close()
        return True

    def photo_exists(self, file_unique_id: str) -> bool:
        with self.get_connection() as conn:
            cur = conn.cursor(); cur.execute(self._sql("SELECT 1 FROM photos WHERE file_unique_id=? LIMIT 1"), (file_unique_id,)); found = cur.fetchone() is not None; cur.close()
        return found

    def add_photo(self, file_id: str, file_unique_id: str, file_size: int, chat_id: int, user_id: int) -> bool:
        with self.get_connection() as conn:
            cur = conn.cursor()
            try:
                cur.execute(self._sql("INSERT INTO photos(file_id,file_unique_id,file_size,chat_id,user_id) VALUES(?,?,?,?,?)"), (file_id,file_unique_id,file_size,chat_id,user_id))
            except Exception as exc:
                if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
                    conn.rollback(); cur.close(); return False
                raise
            cur.close()
        return True

    def get_photo(self, file_unique_id: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = self._cursor(conn); cur.execute(self._sql("SELECT file_id,file_unique_id,file_size FROM photos WHERE file_unique_id=?"), (file_unique_id,)); row = cur.fetchone(); cur.close()
        return self._dict(row)

    def search_photos(self, query: str, user_id: int, limit: int = 10) -> List[Dict[str, Any]]:
        q = f"%{query.strip()}%"
        with self.get_connection() as conn:
            cur = self._cursor(conn); cur.execute(self._sql("SELECT file_id,file_unique_id,file_size,created_at FROM photos WHERE user_id=? AND (file_unique_id LIKE ? OR file_id LIKE ? OR CAST(file_size AS TEXT) LIKE ?) ORDER BY id DESC LIMIT ?"), (user_id,q,q,q,limit)); rows = cur.fetchall(); cur.close()
        keys = ["file_id","file_unique_id","file_size","created_at"]
        return [dict(r) if isinstance(r, dict) else dict(zip(keys,r)) for r in rows]

    def enqueue_video(self, chat_id: int, user_id: int, payload: Dict[str, Any]) -> bool:
        with self.get_connection() as conn:
            cur = conn.cursor()
            try:
                cur.execute(self._sql("INSERT INTO queue_items(chat_id,user_id,file_id,file_unique_id,file_size,duration,width,height,checksum) VALUES(?,?,?,?,?,?,?,?,?)"), (chat_id,user_id,payload["file_id"],payload["file_unique_id"],payload.get("file_size",0),payload.get("duration",0),payload.get("width",0),payload.get("height",0),payload.get("checksum","")))
            except Exception:
                cur.close(); raise
            cur.close()
        return True

    def get_queue(self, chat_id: int, user_id: int) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = self._cursor(conn); cur.execute(self._sql("SELECT file_id,file_unique_id,file_size,duration,width,height,checksum FROM queue_items WHERE chat_id=? AND user_id=? ORDER BY id"), (chat_id,user_id)); rows = cur.fetchall(); cur.close()
        return [dict(r) if isinstance(r, dict) else dict(zip(["file_id","file_unique_id","file_size","duration","width","height","checksum"], r)) for r in rows]

    def remove_queue_items(self, chat_id: int, user_id: int, unique_ids: List[str]) -> int:
        if not unique_ids: return 0
        removed = 0
        with self.get_connection() as conn:
            cur = conn.cursor()
            for uid in unique_ids:
                cur.execute(self._sql("DELETE FROM queue_items WHERE chat_id=? AND user_id=? AND file_unique_id=?"), (chat_id,user_id,uid)); removed += cur.rowcount
            cur.close()
        return removed

    def remove_video_record(self, file_unique_id: str) -> None:
        with self.get_connection() as conn:
            cur = conn.cursor(); cur.execute(self._sql("DELETE FROM videos WHERE file_unique_id=?"), (file_unique_id,)); cur.execute(self._sql("DELETE FROM queue_items WHERE file_unique_id=?"), (file_unique_id,)); cur.close()

    def mark_video_sent(self, file_unique_id: str) -> None:
        with self.get_connection() as conn:
            cur = conn.cursor(); cur.execute(self._sql("UPDATE videos SET status='sent', sent_at=CURRENT_TIMESTAMP WHERE file_unique_id=?"), (file_unique_id,)); cur.close()

    def get_video(self, file_unique_id: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = self._cursor(conn); cur.execute(self._sql("SELECT file_id,file_unique_id,file_size,duration,width,height,status,created_at FROM videos WHERE file_unique_id=?"), (file_unique_id,)); row = cur.fetchone(); cur.close()
        return self._dict(row)

    def search_videos_smart(
        self,
        query: str,
        user_id: int,
        limit: int = 10,
        min_duration: Optional[int] = None,
        exact_duration: Optional[int] = None,
        min_size_mb: Optional[float] = None,
        max_size_mb: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Search by ID text or duration/size filters; size values are MiB."""
        clauses = ["user_id=?"]
        params: List[Any] = [user_id]
        clean_query = (query or "").strip()
        if clean_query:
            if clean_query.isdigit():
                # Numeric search defaults to minimum duration, never substring matching.
                if min_duration is None and exact_duration is None:
                    min_duration = int(clean_query)
            else:
                clauses.append("(file_unique_id LIKE ? OR file_id LIKE ?)")
                needle = f"%{clean_query}%"
                params.extend([needle, needle])
        if exact_duration is not None:
            clauses.append("duration=?")
            params.append(int(exact_duration))
        elif min_duration is not None:
            clauses.append("duration>=?")
            params.append(int(min_duration))
        if min_size_mb is not None:
            clauses.append("file_size>=?")
            params.append(int(float(min_size_mb) * 1024 * 1024))
        if max_size_mb is not None:
            clauses.append("file_size<=?")
            params.append(int(float(max_size_mb) * 1024 * 1024))
        sql = "SELECT file_id,file_unique_id,file_size,duration,width,height,status,created_at FROM videos WHERE " + " AND ".join(clauses) + " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 100)))
        with self.get_connection() as conn:
            cur = self._cursor(conn)
            cur.execute(self._sql(sql), tuple(params))
            rows = cur.fetchall()
            cur.close()
        keys = ["file_id", "file_unique_id", "file_size", "duration", "width", "height", "status", "created_at"]
        return [dict(r) if isinstance(r, dict) else dict(zip(keys, r)) for r in rows]

    def search_videos(self, query: str, user_id: int, limit: int = 10) -> List[Dict[str, Any]]:
        """Backward-compatible wrapper using smart-search behavior."""
        return self.search_videos_smart(query, user_id, limit=limit)

    def get_all_videos(self, user_id: int, limit: int = 10000) -> List[Dict[str, Any]]:
        """Return saved videos for one user in oldest-first order for bulk sending."""
        with self.get_connection() as conn:
            cur = self._cursor(conn)
            cur.execute(self._sql("SELECT file_id,file_unique_id,file_size,duration,width,height,status,created_at FROM videos WHERE user_id=? ORDER BY id ASC LIMIT ?"), (user_id, max(1, min(int(limit), 20000))))
            rows = cur.fetchall()
            cur.close()
        keys = ["file_id", "file_unique_id", "file_size", "duration", "width", "height", "status", "created_at"]
        return [dict(r) if isinstance(r, dict) else dict(zip(keys, r)) for r in rows]

    def is_favorite(self, user_id: int, file_unique_id: str) -> bool:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(self._sql("SELECT 1 FROM favorites WHERE user_id=? AND file_unique_id=? LIMIT 1"), (user_id, file_unique_id))
            found = cur.fetchone() is not None
            cur.close()
        return found

    def toggle_favorite(self, user_id: int, file_unique_id: str) -> bool:
        """Toggle favorite state and return True when the item is now favorited."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(self._sql("SELECT 1 FROM favorites WHERE user_id=? AND file_unique_id=? LIMIT 1"), (user_id, file_unique_id))
            exists = cur.fetchone() is not None
            if exists:
                cur.execute(self._sql("DELETE FROM favorites WHERE user_id=? AND file_unique_id=?"), (user_id, file_unique_id))
                result = False
            else:
                cur.execute(self._sql("INSERT INTO favorites(user_id,file_unique_id) VALUES(?,?)"), (user_id, file_unique_id))
                result = True
            cur.close()
        return result

    def get_counts(self, user_id: Optional[int] = None) -> Dict[str, int]:
        with self.get_connection() as conn:
            cur = conn.cursor()
            if user_id is None:
                cur.execute("SELECT COUNT(*) FROM videos"); videos = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM queue_items"); queued = cur.fetchone()[0]
            else:
                cur.execute(self._sql("SELECT COUNT(*) FROM videos WHERE user_id=?"), (user_id,)); videos = cur.fetchone()[0]
                cur.execute(self._sql("SELECT COUNT(*) FROM queue_items WHERE user_id=?"), (user_id,)); queued = cur.fetchone()[0]
            cur.close()
            if user_id is None:
                cur = conn.cursor(); cur.execute("SELECT COUNT(*) FROM photos"); photos = cur.fetchone()[0]; cur.close()
            else:
                cur = conn.cursor(); cur.execute(self._sql("SELECT COUNT(*) FROM photos WHERE user_id=?"), (user_id,)); photos = cur.fetchone()[0]; cur.close()
        return {"videos": int(videos), "queued": int(queued), "photos": int(photos)}

    # Statistics
    def increment_stat(self, metric: str, amount: int = 1) -> int:
        with self.get_connection() as conn:
            cur = conn.cursor()
            if self.use_postgres:
                cur.execute("INSERT INTO statistics(metric,value) VALUES(%s,%s) ON CONFLICT(metric) DO UPDATE SET value=statistics.value+EXCLUDED.value, updated_at=CURRENT_TIMESTAMP RETURNING value", (metric, amount)); value = cur.fetchone()[0]
            else:
                cur.execute("INSERT INTO statistics(metric,value) VALUES(?,?) ON CONFLICT(metric) DO UPDATE SET value=value+excluded.value, updated_at=CURRENT_TIMESTAMP", (metric, amount)); cur.execute("SELECT value FROM statistics WHERE metric=?", (metric,)); value = cur.fetchone()[0]
            cur.close()
        return int(value)

    def get_stat(self, metric: str) -> int:
        with self.get_connection() as conn:
            cur = conn.cursor(); cur.execute(self._sql("SELECT value FROM statistics WHERE metric=?"), (metric,)); row = cur.fetchone(); cur.close()
        return int((row[0] if row else 0))

    def get_all_stats(self) -> Dict[str, int]:
        with self.get_connection() as conn:
            cur = conn.cursor(); cur.execute("SELECT metric,value FROM statistics"); rows = cur.fetchall(); cur.close()
        return {str(r[0]): int(r[1]) for r in rows}

    def close(self) -> None:
        try:
            if self.use_postgres: self.pg_conn.close()
            elif self.sqlite_conn: self.sqlite_conn.close()
        except Exception:
            logger.exception("Error closing database")


db = Database()
