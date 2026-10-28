"""
PostgreSQL Database Module for Telegram Bot
Handles all database operations with connection pooling
"""

import os
import json
import logging
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# Try to import psycopg2, fall back to sqlite if not available
try:
    import psycopg2
    from psycopg2 import pool, extras
    from psycopg2.extras import RealDictCursor
    POSTGRES_AVAILABLE = True
except ImportError:
    POSTGRES_AVAILABLE = False
    logger.warning("psycopg2 not available, falling back to SQLite")

import sqlite3
import threading


class Database:
    """Database handler with PostgreSQL support and SQLite fallback"""

    def __init__(self):
        self.use_postgres = POSTGRES_AVAILABLE and os.getenv("DATABASE_URL")
        self.connection_pool = None
        self.sqlite_conn = None
        self.sqlite_lock = threading.Lock()
        self._init_database()

    def _init_database(self):
        """Initialize database connection and tables"""
        if self.use_postgres:
            self._init_postgres()
        else:
            self._init_sqlite()
        
        self._create_tables()
        logger.info(f"Database initialized: {'PostgreSQL' if self.use_postgres else 'SQLite'}")

    def _init_postgres(self):
        """Initialize PostgreSQL connection pool"""
        try:
            database_url = os.getenv("DATABASE_URL")
            if database_url.startswith("postgres://"):
                database_url = database_url.replace("postgres://", "postgresql://", 1)
                
            if database_url:
                database_url = database_url.replace("sslmode=Disable", "sslmode=disable")

            self.connection_pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=10,
                dsn=database_url
            )
            logger.info("PostgreSQL connection pool created")
        except Exception as e:
            logger.error(f"PostgreSQL connection failed: {e}, falling back to SQLite")
            self.use_postgres = False
            self._init_sqlite()

    def _init_sqlite(self):
        """Initialize SQLite as fallback"""
        self.sqlite_conn = sqlite3.connect(
            "bot_database.db",
            check_same_thread=False
        )
        self.sqlite_conn.row_factory = sqlite3.Row
        logger.info("SQLite database initialized")

    @contextmanager
    def get_connection(self):
        """Get database connection from pool or SQLite"""
        if self.use_postgres:
            conn = self.connection_pool.getconn()
            try:
                yield conn
                conn.commit()
            except Exception as e:
                conn.rollback()
                raise e
            finally:
                self.connection_pool.putconn(conn)
        else:
            with self.sqlite_lock:
                try:
                    yield self.sqlite_conn
                    self.sqlite_conn.commit()
                except Exception as e:
                    self.sqlite_conn.rollback()
                    raise e

    def _cursor(self, conn):
        """Helper to get a dictionary-returning cursor for both PostgreSQL and SQLite"""
        if self.use_postgres:
            return conn.cursor(cursor_factory=RealDictCursor)
        else:
            return conn.cursor()

    def _create_tables(self):
        """Create necessary tables"""

        users_table = """
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username VARCHAR(255),
                first_name VARCHAR(255),
                last_name VARCHAR(255),
                is_admin BOOLEAN DEFAULT FALSE,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """

        settings_table = """
            CREATE TABLE IF NOT EXISTS settings (
                key VARCHAR(255) PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """

        skipped_videos_table = """
            CREATE TABLE IF NOT EXISTS skipped_videos (
                file_unique_id VARCHAR(255) PRIMARY KEY,
                file_id VARCHAR(255) NOT NULL,
                reason TEXT,
                source VARCHAR(50),
                attempts INTEGER DEFAULT 1,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """

        videos_table = """
            CREATE TABLE IF NOT EXISTS videos (
                id SERIAL PRIMARY KEY,
                file_id VARCHAR(255) UNIQUE NOT NULL,
                file_unique_id VARCHAR(255) UNIQUE NOT NULL,
                checksum VARCHAR(64),
                file_size BIGINT,
                duration INTEGER,
                width INTEGER,
                height INTEGER,
                chat_id BIGINT,
                user_id BIGINT,
                status VARCHAR(50) DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                sent_at TIMESTAMP
            )
        """

        photos_table = """
            CREATE TABLE IF NOT EXISTS photos (
                id SERIAL PRIMARY KEY,
                file_id VARCHAR(255) UNIQUE NOT NULL,
                file_unique_id VARCHAR(255) UNIQUE NOT NULL,
                chat_id BIGINT,
                user_id BIGINT,
                status VARCHAR(50) DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """

        stats_table = """
            CREATE TABLE IF NOT EXISTS statistics (
                id SERIAL PRIMARY KEY,
                metric VARCHAR(255) UNIQUE NOT NULL,
                value BIGINT DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """

        documents_table = """
            CREATE TABLE IF NOT EXISTS documents (
                id SERIAL PRIMARY KEY,
                file_id VARCHAR(255) UNIQUE NOT NULL,
                file_unique_id VARCHAR(255) UNIQUE NOT NULL,
                file_name VARCHAR(255),
                file_size BIGINT,
                mime_type VARCHAR(255),
                chat_id BIGINT,
                user_id BIGINT,
                status VARCHAR(50) DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """

        approvals_table = """
            CREATE TABLE IF NOT EXISTS pending_approvals (
                id SERIAL PRIMARY KEY,
                media_type VARCHAR(50) NOT NULL,
                file_id VARCHAR(255) NOT NULL,
                chat_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                status VARCHAR(50) DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """

        queue_table = """
            CREATE TABLE IF NOT EXISTS queue_items (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                file_id VARCHAR(255) NOT NULL,
                file_unique_id VARCHAR(255) NOT NULL,
                file_size BIGINT DEFAULT 0,
                duration INTEGER DEFAULT 0,
                width INTEGER DEFAULT 0,
                height INTEGER DEFAULT 0,
                checksum VARCHAR(64),
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """

        favorites_table = """
            CREATE TABLE IF NOT EXISTS favorites (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                media_type VARCHAR(20) NOT NULL,
                file_unique_id VARCHAR(255) NOT NULL,
                file_id VARCHAR(255) NOT NULL,
                label VARCHAR(255),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, media_type, file_unique_id)
            )
        """

        with self.get_connection() as conn:
            cursor = conn.cursor()

            if not self.use_postgres:
                for table_name in ['users_table', 'settings_table', 'skipped_videos_table', 'videos_table', 'photos_table', 'stats_table', 'documents_table', 'approvals_table', 'queue_table', 'favorites_table']:
                    locals()[table_name] = locals()[table_name].replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
                    locals()[table_name] = locals()[table_name].replace("BIGINT", "INTEGER")
                    locals()[table_name] = locals()[table_name].replace("BOOLEAN", "INTEGER")
                    locals()[table_name] = locals()[table_name].replace("TIMESTAMP", "TEXT")
                    locals()[table_name] = locals()[table_name].replace("DEFAULT CURRENT_TIMESTAMP", "DEFAULT (datetime('now'))")
                    locals()[table_name] = locals()[table_name].replace("DEFAULT CURRENT_TEXT", "DEFAULT (datetime('now'))")

                users_table = locals()['users_table']
                settings_table = locals()['settings_table']
                skipped_videos_table = locals()['skipped_videos_table']
                videos_table = locals()['videos_table']
                photos_table = locals()['photos_table']
                stats_table = locals()['stats_table']
                documents_table = locals()['documents_table']
                approvals_table = locals()['approvals_table']
                queue_table = locals()['queue_table']
                favorites_table = locals()['favorites_table']

            cursor.execute(users_table)
            cursor.execute(settings_table)
            cursor.execute(videos_table)
            cursor.execute(photos_table)
            cursor.execute(stats_table)
            cursor.execute(documents_table)
            cursor.execute(approvals_table)
            cursor.execute(queue_table)
            cursor.execute(favorites_table)
            cursor.execute(skipped_videos_table)

            default_stats = [
                ("total_videos", 0),
                ("total_photos", 0),
                ("total_documents", 0),
                ("total_batches", 0),
                ("rejected_photos", 0),
                ("rejected_documents", 0),
                ("duplicate_videos", 0),
                ("duplicate_photos", 0),
                ("duplicate_documents", 0),
                ("skipped_photos_disabled", 0),
                ("skipped_documents_disabled", 0),
                ("bot_start_time", int(datetime.now().timestamp()))
            ]

            for metric, value in default_stats:
                if self.use_postgres:
                    cursor.execute(
                        "INSERT INTO statistics (metric, value) VALUES (%s, %s) ON CONFLICT (metric) DO NOTHING",
                        (metric, value)
                    )
                else:
                    cursor.execute(
                        "INSERT OR IGNORE INTO statistics (metric, value) VALUES (?, ?)",
                        (metric, value)
                    )

    # --- Helper & Integration Methods ---

    def get_all_settings(self) -> Dict[str, Any]:
        with self.get_connection() as conn:
            cursor = self._cursor(conn)
            cursor.execute("SELECT key, value FROM settings")
            rows = cursor.fetchall()
            settings = {}
            for row in rows:
                key, val = row["key"], row["value"]
                try:
                    settings[key] = json.loads(val)
                except Exception:
                    settings[key] = val
            return settings

    def set_setting(self, key: str, value: Any):
        val_str = json.dumps(value) if not isinstance(value, str) else value
        with self.get_connection() as conn:
            cursor = self._cursor(conn)
            if self.use_postgres:
                cursor.execute(
                    "INSERT INTO settings (key, value, updated_at) VALUES (%s, %s, CURRENT_TIMESTAMP) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = CURRENT_TIMESTAMP",
                    (key, val_str)
                )
            else:
                cursor.execute(
                    "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, datetime('now'))",
                    (key, val_str)
                )

    def is_admin(self, user_id: int) -> bool:
        with self.get_connection() as conn:
            cursor = self._cursor(conn)
            if self.use_postgres:
                cursor.execute("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
            else:
                cursor.execute("SELECT is_admin FROM users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            return bool(row["is_admin"]) if row else False

    def add_admin(self, user_id: int, username: str = None, first_name: str = None, last_name: str = None) -> bool:
        with self.get_connection() as conn:
            cursor = self._cursor(conn)
            if self.use_postgres:
                cursor.execute(
                    "INSERT INTO users (user_id, username, first_name, last_name, is_admin) VALUES (%s, %s, %s, %s, TRUE) ON CONFLICT (user_id) DO UPDATE SET is_admin = TRUE, username = EXCLUDED.username, first_name = EXCLUDED.first_name, last_name = EXCLUDED.last_name",
                    (user_id, username, first_name, last_name)
                )
            else:
                cursor.execute(
                    "INSERT OR REPLACE INTO users (user_id, username, first_name, last_name, is_admin) VALUES (?, ?, ?, ?, 1)",
                    (user_id, username, first_name, last_name)
                )
            return True

    def remove_admin(self, user_id: int) -> bool:
        with self.get_connection() as conn:
            cursor = self._cursor(conn)
            if self.use_postgres:
                cursor.execute("UPDATE users SET is_admin = FALSE WHERE user_id = %s", (user_id,))
            else:
                cursor.execute("UPDATE users SET is_admin = 0 WHERE user_id = ?", (user_id,))
            return True

    def get_all_admins(self) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cursor = self._cursor(conn)
            if self.use_postgres:
                cursor.execute("SELECT user_id, username, first_name, last_name FROM users WHERE is_admin = TRUE")
            else:
                cursor.execute("SELECT user_id, username, first_name, last_name FROM users WHERE is_admin = 1")
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def update_user_activity(self, user_id: int, username: str = None, first_name: str = None, last_name: str = None):
        with self.get_connection() as conn:
            cursor = self._cursor(conn)
            if self.use_postgres:
                cursor.execute(
                    "INSERT INTO users (user_id, username, first_name, last_name, last_seen) VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP) ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username, first_name = EXCLUDED.first_name, last_name = EXCLUDED.last_name, last_seen = CURRENT_TIMESTAMP",
                    (user_id, username, first_name, last_name)
                )
            else:
                cursor.execute(
                    "INSERT INTO users (user_id, username, first_name, last_name, last_seen) VALUES (?, ?, ?, ?, datetime('now')) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name, last_name=excluded.last_name, last_seen=datetime('now')",
                    (user_id, username, first_name, last_name)
                )

    def get_stat(self, metric: str) -> int:
        with self.get_connection() as conn:
            cursor = self._cursor(conn)
            if self.use_postgres:
                cursor.execute("SELECT value FROM statistics WHERE metric = %s", (metric,))
            else:
                cursor.execute("SELECT value FROM statistics WHERE metric = ?", (metric,))
            row = cursor.fetchone()
            return row["value"] if row else 0

    def get_all_stats(self, *args, **kwargs) -> Dict[str, int]:
        with self.get_connection() as conn:
            cursor = self._cursor(conn)
            cursor.execute("SELECT metric, value FROM statistics")
            rows = cursor.fetchall()
            return {row["metric"]: row["value"] for row in rows}

    def get_health_summary(self) -> Dict[str, Any]:
        with self.get_connection() as conn:
            cursor = self._cursor(conn)
            cursor.execute("SELECT COUNT(*) as count FROM queue_items")
            row = cursor.fetchone()
            queued = row["count"] if row else 0
            return {"queued": queued}

    def get_queue(self, *args, **kwargs) -> List[Dict[str, Any]]:
        chat_id = args[0] if args else kwargs.get("chat_id")
        with self.get_connection() as conn:
            cursor = self._cursor(conn)
            if chat_id is not None:
                if self.use_postgres:
                    cursor.execute("SELECT * FROM queue_items WHERE chat_id = %s ORDER BY added_at ASC", (chat_id,))
                else:
                    cursor.execute("SELECT * FROM queue_items WHERE chat_id = ? ORDER BY added_at ASC", (chat_id,))
            else:
                cursor.execute("SELECT * FROM queue_items ORDER BY added_at ASC")
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def search_files(self, *args, **kwargs) -> List[Dict[str, Any]]:
        query = args[0] if args else kwargs.get("query", "")
        with self.get_connection() as conn:
            cursor = self._cursor(conn)
            if self.use_postgres:
                cursor.execute(
                    "SELECT file_id, file_unique_id, file_name, 'document' as media_type FROM documents WHERE file_name ILIKE %s UNION SELECT file_id, file_unique_id, 'Video' as file_name, 'video' as media_type FROM videos WHERE file_id ILIKE %s LIMIT 20",
                    (f"%{query}%", f"%{query}%")
                )
            else:
                cursor.execute(
                    "SELECT file_id, file_unique_id, file_name, 'document' as media_type FROM documents WHERE file_name LIKE ? UNION SELECT file_id, file_unique_id, 'Video' as file_name, 'video' as media_type FROM videos WHERE file_id LIKE ? LIMIT 20",
                    (f"%{query}%", f"%{query}%")
                )
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def search(self, *args, **kwargs) -> List[Dict[str, Any]]:
        return self.search_files(*args, **kwargs)

    def get_expired_queues(self, timeout: int) -> List[Any]:
        """Finds queues/chat owners whose items have exceeded the flush timeout"""
        with self.get_connection() as conn:
            cursor = self._cursor(conn)
            if self.use_postgres:
                cursor.execute(
                    f"SELECT DISTINCT chat_id FROM queue_items WHERE added_at < CURRENT_TIMESTAMP - INTERVAL '{int(timeout)} seconds'"
                )
            else:
                cursor.execute(
                    "SELECT DISTINCT chat_id FROM queue_items WHERE datetime(added_at) <= datetime('now', ?)",
                    (f"-{timeout} seconds",)
                )
            rows = cursor.fetchall()
            return [row["chat_id"] for row in rows]


db = Database()
