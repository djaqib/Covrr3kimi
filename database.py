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
            f"CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT, is_admin {bool_type} DEFAULT FALSE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
            "CREATE TABLE IF NOT EXISTS video_tags (file_unique_id TEXT NOT NULL, tag TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(file_unique_id, tag))",
            "CREATE TABLE IF NOT EXISTS captions (file_unique_id TEXT PRIMARY KEY, caption TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
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

    def remove_admin(self, user_id: int) -> bool:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(self._sql("UPDATE users SET is_admin=FALSE WHERE user_id=?"), (user_id,))
            changed = cur.rowcount > 0
            cur.close()
        return changed

    def list_admins(self) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = self._cursor(conn)
            cur.execute(self._sql("SELECT user_id, username, first_name FROM users WHERE is_admin=TRUE ORDER BY user_id"))
            rows = cur.fetchall()
            cur.close()
        return [dict(r) if isinstance(r, dict) else dict(zip(["user_id","username","first_name"], r)) for r in rows]

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

    def delete_photo(self, file_unique_id: str) -> bool:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(self._sql("DELETE FROM photos WHERE file_unique_id=?"), (file_unique_id,))
            changed = cur.rowcount > 0
            cur.execute(self._sql("DELETE FROM video_tags WHERE file_unique_id=?"), (file_unique_id,))
            cur.execute(self._sql("DELETE FROM captions WHERE file_unique_id=?"), (file_unique_id,))
            cur.execute(self._sql("DELETE FROM favorites WHERE file_unique_id=?"), (file_unique_id,))
            cur.close()
        return changed

    def search_photos(self, query: str, limit: int = 10, offset: int = 0) -> List[Dict[str, Any]]:
        q = f"%{query.strip()}%"
        with self.get_connection() as conn:
            cur = self._cursor(conn); cur.execute(self._sql("SELECT file_id,file_unique_id,file_size,created_at FROM photos WHERE (file_unique_id LIKE ? OR file_id LIKE ?) ORDER BY id DESC LIMIT ? OFFSET ?"), (q,q,limit,offset)); rows = cur.fetchall(); cur.close()
        keys = ["file_id","file_unique_id","file_size","created_at"]
        return [dict(r) if isinstance(r, dict) else dict(zip(keys,r)) for r in rows]

    def count_photos(self, query: str = "") -> int:
        with self.get_connection() as conn:
            cur = conn.cursor()
            if query:
                q = f"%{query.strip()}%"
                cur.execute(self._sql("SELECT COUNT(*) FROM photos WHERE (file_unique_id LIKE ? OR file_id LIKE ?)"), (q, q))
            else:
                cur.execute("SELECT COUNT(*) FROM photos")
            row = cur.fetchone(); cur.close()
        return int(row[0] if row else 0)

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

    def delete_video(self, file_unique_id: str) -> bool:
        """Fully delete a video from the library, queue, tags, captions and favorites."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(self._sql("DELETE FROM videos WHERE file_unique_id=?"), (file_unique_id,))
            changed = cur.rowcount > 0
            cur.execute(self._sql("DELETE FROM queue_items WHERE file_unique_id=?"), (file_unique_id,))
            cur.execute(self._sql("DELETE FROM video_tags WHERE file_unique_id=?"), (file_unique_id,))
            cur.execute(self._sql("DELETE FROM captions WHERE file_unique_id=?"), (file_unique_id,))
            cur.execute(self._sql("DELETE FROM favorites WHERE file_unique_id=?"), (file_unique_id,))
            cur.close()
        return changed

    def mark_video_sent(self, file_unique_id: str) -> None:
        with self.get_connection() as conn:
            cur = conn.cursor(); cur.execute(self._sql("UPDATE videos SET status='sent', sent_at=CURRENT_TIMESTAMP WHERE file_unique_id=?"), (file_unique_id,)); cur.close()

    def get_video(self, file_unique_id: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = self._cursor(conn); cur.execute(self._sql("SELECT file_id,file_unique_id,file_size,duration,width,height,status,created_at FROM videos WHERE file_unique_id=?"), (file_unique_id,)); row = cur.fetchone(); cur.close()
        return self._dict(row)

    def get_video_tags(self, file_unique_id: str) -> List[str]:
        with self.get_connection() as conn:
            cur = self._cursor(conn)
            cur.execute(self._sql("SELECT tag FROM video_tags WHERE file_unique_id=? ORDER BY tag"), (file_unique_id,))
            rows = cur.fetchall(); cur.close()
        return [r["tag"] if isinstance(r, dict) else r[0] for r in rows]

    def add_tag(self, file_unique_id: str, tag: str) -> None:
        tag = tag.strip().lower()
        if not tag: return
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(self._sql("INSERT OR IGNORE INTO video_tags(file_unique_id,tag) VALUES(?,?)") if not self.use_postgres else self._sql("INSERT INTO video_tags(file_unique_id,tag) VALUES(?,?) ON CONFLICT DO NOTHING"), (file_unique_id, tag))
            cur.close()

    def remove_tag(self, file_unique_id: str, tag: str) -> bool:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(self._sql("DELETE FROM video_tags WHERE file_unique_id=? AND tag=?"), (file_unique_id, tag.strip().lower()))
            changed = cur.rowcount > 0
            cur.close()
        return changed

    def set_caption(self, file_unique_id: str, caption: str) -> None:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(self._sql("INSERT INTO captions(file_unique_id,caption) VALUES(?,?) ON CONFLICT(file_unique_id) DO UPDATE SET caption=EXCLUDED.caption") if self.use_postgres else self._sql("INSERT INTO captions(file_unique_id,caption) VALUES(?,?) ON CONFLICT(file_unique_id) DO UPDATE SET caption=excluded.caption"), (file_unique_id, caption))
            cur.close()

    def get_caption(self, file_unique_id: str) -> Optional[str]:
        with self.get_connection() as conn:
            cur = self._cursor(conn)
            cur.execute(self._sql("SELECT caption FROM captions WHERE file_unique_id=?"), (file_unique_id,))
            row = cur.fetchone(); cur.close()
        if not row: return None
        return row["caption"] if isinstance(row, dict) else row[0]

    def list_tags(self) -> List[str]:
        with self.get_connection() as conn:
            cur = self._cursor(conn)
            cur.execute("SELECT tag, COUNT(*) AS cnt FROM video_tags GROUP BY tag ORDER BY cnt DESC, tag")
            rows = cur.fetchall(); cur.close()
        return [r["tag"] if isinstance(r, dict) else r[0] for r in rows]

    def search_videos_smart(
        self,
        query: str,
        limit: int = 50,
        offset: int = 0,
        min_duration: Optional[int] = None,
        exact_duration: Optional[int] = None,
        min_size_mb: Optional[float] = None,
        max_size_mb: Optional[float] = None,
        tag: Optional[str] = None,
        favorites_of: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Search by ID text, duration/size filters, tag, or favorites. Shared library: no user scoping."""
        clauses = ["1=1"]
        params: List[Any] = []
        clean_query = (query or "").strip()
        if clean_query:
            if clean_query.isdigit():
                if min_duration is None and exact_duration is None:
                    min_duration = int(clean_query)
            else:
                clauses.append("(v.file_unique_id LIKE ? OR v.file_id LIKE ?)")
                needle = f"%{clean_query}%"
                params.extend([needle, needle])
        if exact_duration is not None:
            clauses.append("v.duration=?")
            params.append(int(exact_duration))
        elif min_duration is not None:
            clauses.append("v.duration>=?")
            params.append(int(min_duration))
        if min_size_mb is not None:
            clauses.append("v.file_size>=?")
            params.append(int(float(min_size_mb) * 1024 * 1024))
        if max_size_mb is not None:
            clauses.append("v.file_size<=?")
            params.append(int(float(max_size_mb) * 1024 * 1024))
        if tag:
            clauses.append("EXISTS (SELECT 1 FROM video_tags t WHERE t.file_unique_id=v.file_unique_id AND t.tag=?)")
            params.append(tag.strip().lower())
        if favorites_of is not None:
            clauses.append("EXISTS (SELECT 1 FROM favorites f WHERE f.file_unique_id=v.file_unique_id AND f.user_id=?)")
            params.append(favorites_of)
        sql = ("SELECT v.file_id,v.file_unique_id,v.file_size,v.duration,v.width,v.height,v.status,v.created_at "
               "FROM videos v WHERE " + " AND ".join(clauses) + " ORDER BY v.id DESC LIMIT ? OFFSET ?")
        params.append(max(1, min(int(limit), 100)))
        params.append(max(0, int(offset)))
        with self.get_connection() as conn:
            cur = self._cursor(conn)
            cur.execute(self._sql(sql), tuple(params))
            rows = cur.fetchall()
            cur.close()
        keys = ["file_id", "file_unique_id", "file_size", "duration", "width", "height", "status", "created_at"]
        return [dict(r) if isinstance(r, dict) else dict(zip(keys, r)) for r in rows]

    def count_videos_smart(
        self,
        query: str = "",
        min_duration: Optional[int] = None,
        exact_duration: Optional[int] = None,
        min_size_mb: Optional[float] = None,
        max_size_mb: Optional[float] = None,
        tag: Optional[str] = None,
        favorites_of: Optional[int] = None,
    ) -> int:
        clauses = ["1=1"]
        params: List[Any] = []
        clean_query = (query or "").strip()
        if clean_query:
            if clean_query.isdigit():
                if min_duration is None and exact_duration is None:
                    min_duration = int(clean_query)
            else:
                clauses.append("(v.file_unique_id LIKE ? OR v.file_id LIKE ?)")
                needle = f"%{clean_query}%"
                params.extend([needle, needle])
        if exact_duration is not None:
            clauses.append("v.duration=?"); params.append(int(exact_duration))
        elif min_duration is not None:
            clauses.append("v.duration>=?"); params.append(int(min_duration))
        if min_size_mb is not None:
            clauses.append("v.file_size>=?"); params.append(int(float(min_size_mb) * 1024 * 1024))
        if max_size_mb is not None:
            clauses.append("v.file_size<=?"); params.append(int(float(max_size_mb) * 1024 * 1024))
        if tag:
            clauses.append("EXISTS (SELECT 1 FROM video_tags t WHERE t.file_unique_id=v.file_unique_id AND t.tag=?)")
            params.append(tag.strip().lower())
        if favorites_of is not None:
            clauses.append("EXISTS (SELECT 1 FROM favorites f WHERE f.file_unique_id=v.file_unique_id AND f.user_id=?)")
            params.append(favorites_of)
        sql = "SELECT COUNT(*) FROM videos v WHERE " + " AND ".join(clauses)
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(self._sql(sql), tuple(params))
            row = cur.fetchone(); cur.close()
        return int(row[0] if row else 0)

    def search_videos(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Backward-compatible wrapper using smart-search behavior (shared library)."""
        return self.search_videos_smart(query, limit=limit)

    def get_all_videos(self, limit: int = 10000, offset: int = 0) -> List[Dict[str, Any]]:
        """Return saved videos in oldest-first order for bulk sending. Shared library."""
        with self.get_connection() as conn:
            cur = self._cursor(conn)
            cur.execute(self._sql("SELECT file_id,file_unique_id,file_size,duration,width,height,status,created_at FROM videos ORDER BY id ASC LIMIT ? OFFSET ?"), (max(1, min(int(limit), 20000)), max(0, int(offset))))
            rows = cur.fetchall()
            cur.close()
        keys = ["file_id", "file_unique_id", "file_size", "duration", "width", "height", "status", "created_at"]
        return [dict(r) if isinstance(r, dict) else dict(zip(keys, r)) for r in rows]

    def get_recent_videos(self, limit: int = 40, offset: int = 0) -> List[Dict[str, Any]]:
        """Return the newest saved videos first (for /recent)."""
        with self.get_connection() as conn:
            cur = self._cursor(conn)
            cur.execute(self._sql("SELECT file_id,file_unique_id,file_size,duration,width,height,status,created_at FROM videos ORDER BY id DESC LIMIT ? OFFSET ?"), (max(1, min(int(limit), 100)), max(0, int(offset))))
            rows = cur.fetchall()
            cur.close()
        keys = ["file_id", "file_unique_id", "file_size", "duration", "width", "height", "status", "created_at"]
        return [dict(r) if isinstance(r, dict) else dict(zip(keys, r)) for r in rows]

    def get_all_photo_refs(self, limit: int = 50000) -> List[Dict[str, Any]]:
        """Return photo file refs for dead-file scanning."""
        with self.get_connection() as conn:
            cur = self._cursor(conn)
            cur.execute(self._sql("SELECT file_id,file_unique_id FROM photos ORDER BY id ASC LIMIT ?"), (max(1, min(int(limit), 100000)),))
            rows = cur.fetchall()
            cur.close()
        keys = ["file_id", "file_unique_id"]
        return [dict(r) if isinstance(r, dict) else dict(zip(keys, r)) for r in rows]

    def get_random_video(self, tag: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = self._cursor(conn)
            if tag:
                cur.execute(self._sql("SELECT v.file_id,v.file_unique_id,v.file_size,v.duration,v.width,v.height,v.status,v.created_at FROM videos v WHERE EXISTS (SELECT 1 FROM video_tags t WHERE t.file_unique_id=v.file_unique_id AND t.tag=?) ORDER BY RANDOM() LIMIT 1"), (tag.strip().lower(),))
            else:
                cur.execute("SELECT file_id,file_unique_id,file_size,duration,width,height,status,created_at FROM videos ORDER BY RANDOM() LIMIT 1")
            row = cur.fetchone(); cur.close()
        return self._dict(row)

    def get_total_size(self) -> int:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COALESCE(SUM(file_size),0) FROM videos")
            row = cur.fetchone(); cur.close()
        return int(row[0] if row else 0)

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
        """Shared-library counts. user_id only affects queue counts."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM videos"); videos = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM photos"); photos = cur.fetchone()[0]
            if user_id is None:
                cur.execute("SELECT COUNT(*) FROM queue_items"); queued = cur.fetchone()[0]
            else:
                cur.execute(self._sql("SELECT COUNT(*) FROM queue_items WHERE user_id=?"), (user_id,)); queued = cur.fetchone()[0]
            cur.close()
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

    def export_library(self) -> Dict[str, Any]:
        """Export the full shared library as a JSON-serializable dict."""
        with self.get_connection() as conn:
            cur = self._cursor(conn)
            cur.execute("SELECT file_id,file_unique_id,file_size,duration,width,height,status,created_at FROM videos ORDER BY id")
            vids = cur.fetchall()
            cur.execute("SELECT file_id,file_unique_id,file_size,created_at FROM photos ORDER BY id")
            pics = cur.fetchall()
            cur.execute("SELECT file_unique_id, tag FROM video_tags ORDER BY tag")
            tags = cur.fetchall()
            cur.execute("SELECT file_unique_id, caption FROM captions")
            caps = cur.fetchall()
            cur.close()
        vk = ["file_id","file_unique_id","file_size","duration","width","height","status","created_at"]
        pk = ["file_id","file_unique_id","file_size","created_at"]
        return {
            "videos": [dict(r) if isinstance(r, dict) else dict(zip(vk, r)) for r in vids],
            "photos": [dict(r) if isinstance(r, dict) else dict(zip(pk, r)) for r in pics],
            "tags": [dict(r) if isinstance(r, dict) else dict(zip(["file_unique_id","tag"], r)) for r in tags],
            "captions": [dict(r) if isinstance(r, dict) else dict(zip(["file_unique_id","caption"], r)) for r in caps],
        }

    def get_recent_videos(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Return the most recently added videos (newest first)."""
        with self.get_connection() as conn:
            cur = self._cursor(conn)
            cur.execute(self._sql("SELECT file_id,file_unique_id,file_size,duration,width,height,status,created_at FROM videos ORDER BY id DESC LIMIT ?"), (max(1, min(int(limit), 50)),))
            rows = cur.fetchall()
            cur.close()
        keys = ["file_id", "file_unique_id", "file_size", "duration", "width", "height", "status", "created_at"]
        return [dict(r) if isinstance(r, dict) else dict(zip(keys, r)) for r in rows]

    def get_recent_photos(self, limit: int = 5) -> List[Dict[str, Any]]:
        """Return the most recently added photos (newest first)."""
        with self.get_connection() as conn:
            cur = self._cursor(conn)
            cur.execute(self._sql("SELECT file_id,file_unique_id,file_size,created_at FROM photos ORDER BY id DESC LIMIT ?"), (max(1, min(int(limit), 50)),))
            rows = cur.fetchall()
            cur.close()
        keys = ["file_id", "file_unique_id", "file_size", "created_at"]
        return [dict(r) if isinstance(r, dict) else dict(zip(keys, r)) for r in rows]

    def get_all_photos(self, limit: int = 10000) -> List[Dict[str, Any]]:
        """Return all saved photos (oldest first) for scanning."""
        with self.get_connection() as conn:
            cur = self._cursor(conn)
            cur.execute(self._sql("SELECT file_id,file_unique_id,file_size,created_at FROM photos ORDER BY id ASC LIMIT ?"), (max(1, min(int(limit), 20000)),))
            rows = cur.fetchall()
            cur.close()
        keys = ["file_id", "file_unique_id", "file_size", "created_at"]
        return [dict(r) if isinstance(r, dict) else dict(zip(keys, r)) for r in rows]

    def delete_videos_batch(self, unique_ids: List[str]) -> int:
        """Delete multiple videos fully (library, queue, tags, captions). Returns count removed."""
        removed = 0
        with self.get_connection() as conn:
            cur = conn.cursor()
            for uid in unique_ids:
                cur.execute(self._sql("DELETE FROM videos WHERE file_unique_id=?"), (uid,))
                removed += cur.rowcount
                cur.execute(self._sql("DELETE FROM queue_items WHERE file_unique_id=?"), (uid,))
                cur.execute(self._sql("DELETE FROM video_tags WHERE file_unique_id=?"), (uid,))
                cur.execute(self._sql("DELETE FROM captions WHERE file_unique_id=?"), (uid,))
            cur.close()
        return removed

    def delete_photos_batch(self, unique_ids: List[str]) -> int:
        """Delete multiple photos. Returns count removed."""
        removed = 0
        with self.get_connection() as conn:
            cur = conn.cursor()
            for uid in unique_ids:
                cur.execute(self._sql("DELETE FROM photos WHERE file_unique_id=?"), (uid,))
                removed += cur.rowcount
            cur.close()
        return removed

    def find_duplicates(self) -> List[List[Dict[str, Any]]]:
        """Group videos by checksum (size+duration+width+height). Returns groups with 2+ members, oldest first."""
        with self.get_connection() as conn:
            cur = self._cursor(conn)
            cur.execute(self._sql("SELECT file_unique_id,checksum,file_size,duration,status,created_at FROM videos WHERE checksum IS NOT NULL AND checksum != '' ORDER BY id ASC"))
            rows = cur.fetchall()
            cur.close()
        keys = ["file_unique_id", "checksum", "file_size", "duration", "status", "created_at"]
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            d = dict(r) if isinstance(r, dict) else dict(zip(keys, r))
            groups.setdefault(d["checksum"], []).append(d)
        return [g for g in groups.values() if len(g) > 1]

    def close(self) -> None:
        try:
            if self.use_postgres: self.pg_conn.close()
            elif self.sqlite_conn: self.sqlite_conn.close()
        except Exception:
            logger.exception("Error closing database")


db = Database()
