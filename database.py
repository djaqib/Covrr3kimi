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
            # Handle Railway/Heroku format
            if database_url.startswith("postgres://"):
                database_url = database_url.replace("postgres://", "postgresql://", 1)
            
            # ---> ADD THIS LINE TO FIX THE SSLMODE ERROR <---
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

    def _create_tables(self):
        """Create necessary tables"""

        # Users table (for admin management)
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

        # Settings table (persistent configuration)
        settings_table = """
            CREATE TABLE IF NOT EXISTS settings (
                key VARCHAR(255) PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """
        # skipped_videos_table
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
      
        # Videos table (track processed videos)
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

        # Photos table (track processed photos)
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

        # Statistics table
        stats_table = """
            CREATE TABLE IF NOT EXISTS statistics (
                id SERIAL PRIMARY KEY,
                metric VARCHAR(255) UNIQUE NOT NULL,
                value BIGINT DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """

        # Document/Files table
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

        # Pending approvals table
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

            # Adjust syntax for SQLite
            if not self.use_postgres:
                users_table = users_table.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
                users_table = users_table.replace("BIGINT", "INTEGER")
                users_table = users_table.replace("BOOLEAN", "INTEGER")
                users_table = users_table.replace("TIMESTAMP", "TEXT")
                users_table = users_table.replace("DEFAULT CURRENT_TIMESTAMP", "DEFAULT (datetime('now'))")

                settings_table = settings_table.replace("TIMESTAMP", "TEXT")
                settings_table = settings_table.replace("DEFAULT CURRENT_TIMESTAMP", "DEFAULT (datetime('now'))")

                videos_table = videos_table.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
                videos_table = videos_table.replace("BIGINT", "INTEGER")
                videos_table = videos_table.replace("TIMESTAMP", "TEXT")
                videos_table = videos_table.replace("DEFAULT CURRENT_TIMESTAMP", "DEFAULT (datetime('now'))")

                photos_table = photos_table.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
                photos_table = photos_table.replace("BIGINT", "INTEGER")
                photos_table = photos_table.replace("TIMESTAMP", "TEXT")
                photos_table = photos_table.replace("DEFAULT CURRENT_TIMESTAMP", "DEFAULT (datetime('now'))")

                stats_table = stats_table.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
                stats_table = stats_table.replace("BIGINT", "INTEGER")
                stats_table = stats_table.replace("TIMESTAMP", "TEXT")
                stats_table = stats_table.replace("DEFAULT CURRENT_TIMESTAMP", "DEFAULT (datetime('now'))")

                documents_table = documents_table.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
                documents_table = documents_table.replace("BIGINT", "INTEGER")
                documents_table = documents_table.replace("TIMESTAMP", "TEXT")
                documents_table = documents_table.replace("DEFAULT CURRENT_TIMESTAMP", "DEFAULT (datetime('now'))")

                approvals_table = approvals_table.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
                approvals_table = approvals_table.replace("BIGINT", "INTEGER")
                approvals_table = approvals_table.replace("TIMESTAMP", "TEXT")
                approvals_table = approvals_table.replace("DEFAULT CURRENT_TIMESTAMP", "DEFAULT (datetime('now'))")
                queue_table = queue_table.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
                queue_table = queue_table.replace("BIGINT", "INTEGER")
                queue_table = queue_table.replace("TIMESTAMP", "TEXT")
                queue_table = queue_table.replace("DEFAULT CURRENT_TIMESTAMP", "DEFAULT (datetime('now'))")
                favorites_table = favorites_table.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
                favorites_table = favorites_table.replace("BIGINT", "INTEGER")
                favorites_table = favorites_table.replace("TIMESTAMP", "TEXT")
                favorites_table = favorites_table.replace("DEFAULT CURRENT_TIMESTAMP", "DEFAULT (datetime('now'))")
                # Replacing TIMESTAMP also changes CURRENT_TIMESTAMP, so normalize any affected defaults.
                users_table = users_table.replace("DEFAULT CURRENT_TEXT", "DEFAULT (datetime('now'))")
                settings_table = settings_table.replace("DEFAULT CURRENT_TEXT", "DEFAULT (datetime('now'))")
                videos_table = videos_table.replace("DEFAULT CURRENT_TEXT", "DEFAULT (datetime('now'))")
                photos_table = photos_table.replace("DEFAULT CURRENT_TEXT", "DEFAULT (datetime('now'))")
                stats_table = stats_table.replace("DEFAULT CURRENT_TEXT", "DEFAULT (datetime('now'))")
                documents_table = documents_table.replace("DEFAULT CURRENT_TEXT", "DEFAULT (datetime('now'))")
                approvals_table = approvals_table.replace("DEFAULT CURRENT_TEXT", "DEFAULT (datetime('now'))")
                queue_table = queue_table.replace("DEFAULT CURRENT_TEXT", "DEFAULT (datetime('now'))")
                favorites_table = favorites_table.replace("DEFAULT CURRENT_TEXT", "DEFAULT (datetime('now'))")

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
            # Insert default statistics if not exist
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

            cursor.close()

    # ==================== SETTINGS METHODS ====================

    def get_setting(self, key: str, default: Any = None) -> Any:
        """Get a setting value"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if self.use_postgres:
                cursor.execute("SELECT value FROM settings WHERE key = %s", (key,))
            else:
                cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))

            result = cursor.fetchone()
            cursor.close()

            if result:
                try:
                    return json.loads(result[0])
                except:
                    return result[0]
            return default

    def set_setting(self, key: str, value: Any):
        """Set a setting value"""
        value_str = json.dumps(value) if not isinstance(value, str) else value

        with self.get_connection() as conn:
            cursor = conn.cursor()
            if self.use_postgres:
                cursor.execute("""
                    INSERT INTO settings (key, value, updated_at) 
                    VALUES (%s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (key) 
                    DO UPDATE SET value = EXCLUDED.value, updated_at = CURRENT_TIMESTAMP
                """, (key, value_str))
            else:
                cursor.execute("""
                    INSERT INTO settings (key, value, updated_at) 
                    VALUES (?, ?, datetime('now'))
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')
                """, (key, value_str))
            cursor.close()

    def get_all_settings(self) -> Dict[str, Any]:
        """Get all settings"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT key, value FROM settings")
            results = cursor.fetchall()
            cursor.close()

            settings = {}
            for row in results:
                try:
                    settings[row[0]] = json.loads(row[1])
                except:
                    settings[row[0]] = row[1]
            return settings

    # ==================== ADMIN METHODS ====================

    def add_admin(self, user_id: int, username: str = None, first_name: str = None, last_name: str = None) -> bool:
        """Add or update an admin user"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                if self.use_postgres:
                    cursor.execute("""
                        INSERT INTO users (user_id, username, first_name, last_name, is_admin, last_seen)
                        VALUES (%s, %s, %s, %s, TRUE, CURRENT_TIMESTAMP)
                        ON CONFLICT (user_id) 
                        DO UPDATE SET is_admin = TRUE, username = EXCLUDED.username, 
                                     first_name = EXCLUDED.first_name, last_name = EXCLUDED.last_name,
                                     last_seen = CURRENT_TIMESTAMP
                    """, (user_id, username, first_name, last_name))
                else:
                    cursor.execute("""
                        INSERT INTO users (user_id, username, first_name, last_name, is_admin, last_seen)
                        VALUES (?, ?, ?, ?, 1, datetime('now'))
                        ON CONFLICT(user_id) DO UPDATE SET 
                        is_admin = 1, username = excluded.username, 
                        first_name = excluded.first_name, last_name = excluded.last_name,
                        last_seen = datetime('now')
                    """, (user_id, username, first_name, last_name))
                cursor.close()
                return True
        except Exception as e:
            logger.error(f"Error adding admin: {e}")
            return False

    def remove_admin(self, user_id: int) -> bool:
        """Remove admin privileges"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                if self.use_postgres:
                    cursor.execute("UPDATE users SET is_admin = FALSE WHERE user_id = %s", (user_id,))
                else:
                    cursor.execute("UPDATE users SET is_admin = 0 WHERE user_id = ?", (user_id,))
                cursor.close()
                return True
        except Exception as e:
            logger.error(f"Error removing admin: {e}")
            return False

    def is_admin(self, user_id: int) -> bool:
        """Check if user is admin"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if self.use_postgres:
                cursor.execute("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
            else:
                cursor.execute("SELECT is_admin FROM users WHERE user_id = ?", (user_id,))

            result = cursor.fetchone()
            cursor.close()
            return bool(result and result[0])

    def get_all_admins(self) -> List[Dict]:
        """Get all admin users"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if self.use_postgres:
                cursor.execute("""
                    SELECT user_id, username, first_name, last_name, created_at, last_seen 
                    FROM users WHERE is_admin = TRUE ORDER BY created_at
                """)
            else:
                cursor.execute("""
                    SELECT user_id, username, first_name, last_name, created_at, last_seen 
                    FROM users WHERE is_admin = 1 ORDER BY created_at
                """)

            columns = [desc[0] for desc in cursor.description]
            results = cursor.fetchall()
            cursor.close()

            return [dict(zip(columns, row)) for row in results]

    def update_user_activity(self, user_id: int, username: str = None, first_name: str = None, last_name: str = None):
        """Update user's last seen and info"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if self.use_postgres:
                cursor.execute("""
                    INSERT INTO users (user_id, username, first_name, last_name, last_seen)
                    VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (user_id) 
                    DO UPDATE SET last_seen = CURRENT_TIMESTAMP, username = EXCLUDED.username,
                                 first_name = EXCLUDED.first_name, last_name = EXCLUDED.last_name
                """, (user_id, username, first_name, last_name))
            else:
                cursor.execute("""
                    INSERT INTO users (user_id, username, first_name, last_name, last_seen)
                    VALUES (?, ?, ?, ?, datetime('now'))
                    ON CONFLICT(user_id) DO UPDATE SET 
                    last_seen = datetime('now'), username = excluded.username,
                    first_name = excluded.first_name, last_name = excluded.last_name
                """, (user_id, username, first_name, last_name))
            cursor.close()

    # ==================== VIDEO METHODS ====================

    def add_video(self, file_id: str, file_unique_id: str, checksum: str, 
                  file_size: int, duration: int, width: int, height: int,
                  chat_id: int, user_id: int) -> bool:
        """Add a video record"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                if self.use_postgres:
                    cursor.execute("""
                        INSERT INTO videos (file_id, file_unique_id, checksum, file_size, duration, width, height, chat_id, user_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (file_unique_id) DO NOTHING
                    """, (file_id, file_unique_id, checksum, file_size, duration, width, height, chat_id, user_id))
                else:
                    cursor.execute("""
                        INSERT OR IGNORE INTO videos (file_id, file_unique_id, checksum, file_size, duration, width, height, chat_id, user_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (file_id, file_unique_id, checksum, file_size, duration, width, height, chat_id, user_id))
                inserted = cursor.rowcount > 0
                cursor.close()
                return inserted
        except Exception as e:
            logger.error(f"Error adding video: {e}")
            return False

    def video_exists(self, file_unique_id: str = None, checksum: str = None) -> bool:
        """Check if video exists by unique ID or checksum"""
        with self.get_connection() as conn:
            cursor = conn.cursor()

            if file_unique_id:
                if self.use_postgres:
                    cursor.execute("SELECT 1 FROM videos WHERE file_unique_id = %s", (file_unique_id,))
                else:
                    cursor.execute("SELECT 1 FROM videos WHERE file_unique_id = ?", (file_unique_id,))
                result = cursor.fetchone()
                if result:
                    cursor.close()
                    return True

            if checksum:
                if self.use_postgres:
                    cursor.execute("SELECT 1 FROM videos WHERE checksum = %s", (checksum,))
                else:
                    cursor.execute("SELECT 1 FROM videos WHERE checksum = ?", (checksum,))
                result = cursor.fetchone()
                cursor.close()
                return bool(result)

            cursor.close()
            return False

    def remove_video_record(self, file_unique_id: str) -> None:
        """Roll back a just-created record if persisting its queue entry fails."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            if self.use_postgres: cur.execute("DELETE FROM videos WHERE file_unique_id=%s AND status='pending'", (file_unique_id,))
            else: cur.execute("DELETE FROM videos WHERE file_unique_id=? AND status='pending'", (file_unique_id,))
            cur.close()

    def mark_video_sent(self, file_unique_id: str):
        """Mark video as sent"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if self.use_postgres:
                cursor.execute("""
                    UPDATE videos SET status = 'sent', sent_at = CURRENT_TIMESTAMP 
                    WHERE file_unique_id = %s
                """, (file_unique_id,))
            else:
                cursor.execute("""
                    UPDATE videos SET status = 'sent', sent_at = datetime('now') 
                    WHERE file_unique_id = ?
                """, (file_unique_id,))
            cursor.close()

    # ==================== PHOTO METHODS ====================

    def add_photo(self, file_id: str, file_unique_id: str, chat_id: int, user_id: int) -> bool:
        """Add a photo record"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                if self.use_postgres:
                    cursor.execute("""
                        INSERT INTO photos (file_id, file_unique_id, chat_id, user_id)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (file_unique_id) DO NOTHING
                    """, (file_id, file_unique_id, chat_id, user_id))
                else:
                    cursor.execute("""
                        INSERT OR IGNORE INTO photos (file_id, file_unique_id, chat_id, user_id)
                        VALUES (?, ?, ?, ?)
                    """, (file_id, file_unique_id, chat_id, user_id))
                inserted = cursor.rowcount > 0
                cursor.close()
                return inserted
        except Exception as e:
            logger.error(f"Error adding photo: {e}")
            return False

    def photo_exists(self, file_unique_id: str) -> bool:
        """Check if photo exists"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if self.use_postgres:
                cursor.execute("SELECT 1 FROM photos WHERE file_unique_id = %s", (file_unique_id,))
            else:
                cursor.execute("SELECT 1 FROM photos WHERE file_unique_id = ?", (file_unique_id,))
            result = cursor.fetchone()
            cursor.close()
            return bool(result)

    # ==================== DOCUMENT METHODS ====================

    def add_document(self, file_id: str, file_unique_id: str, file_name: str,
                     file_size: int, mime_type: str, chat_id: int, user_id: int) -> bool:
        """Add a document record"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                if self.use_postgres:
                    cursor.execute("""
                        INSERT INTO documents (file_id, file_unique_id, file_name, file_size, mime_type, chat_id, user_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (file_unique_id) DO NOTHING
                    """, (file_id, file_unique_id, file_name, file_size, mime_type, chat_id, user_id))
                else:
                    cursor.execute("""
                        INSERT OR IGNORE INTO documents (file_id, file_unique_id, file_name, file_size, mime_type, chat_id, user_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (file_id, file_unique_id, file_name, file_size, mime_type, chat_id, user_id))
                inserted = cursor.rowcount > 0
                cursor.close()
                return inserted
        except Exception as e:
            logger.error(f"Error adding document: {e}")
            return False

    def document_exists(self, file_unique_id: str) -> bool:
        """Check if document exists"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if self.use_postgres:
                cursor.execute("SELECT 1 FROM documents WHERE file_unique_id = %s", (file_unique_id,))
            else:
                cursor.execute("SELECT 1 FROM documents WHERE file_unique_id = ?", (file_unique_id,))
            result = cursor.fetchone()
            cursor.close()
            return bool(result)

    # ==================== STATISTICS METHODS ====================

    def increment_stat(self, metric: str, amount: int = 1):
        """Increment a statistic"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if self.use_postgres:
                cursor.execute("""
                    INSERT INTO statistics (metric, value) 
                    VALUES (%s, %s)
                    ON CONFLICT (metric) 
                    DO UPDATE SET value = statistics.value + %s, updated_at = CURRENT_TIMESTAMP
                """, (metric, amount, amount))
            else:
                cursor.execute("""
                    INSERT INTO statistics (metric, value) 
                    VALUES (?, ?)
                    ON CONFLICT(metric) DO UPDATE SET 
                    value = value + excluded.value, updated_at = datetime('now')
                """, (metric, amount))
            cursor.close()

    def get_stat(self, metric: str) -> int:
        """Get a statistic value"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if self.use_postgres:
                cursor.execute("SELECT value FROM statistics WHERE metric = %s", (metric,))
            else:
                cursor.execute("SELECT value FROM statistics WHERE metric = ?", (metric,))
            result = cursor.fetchone()
            cursor.close()
            return result[0] if result else 0

    def get_all_stats(self) -> Dict[str, int]:
        """Get all statistics"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT metric, value FROM statistics")
            results = cursor.fetchall()
            cursor.close()
            return {row[0]: row[1] for row in results}

    def reset_stats(self) -> bool:
        """Reset counters without deleting deduplication records or changing uptime."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                if self.use_postgres:
                    cursor.execute(
                        "UPDATE statistics SET value = 0, updated_at = CURRENT_TIMESTAMP "
                        "WHERE metric != %s",
                        ("bot_start_time",),
                    )
                else:
                    cursor.execute(
                        "UPDATE statistics SET value = 0, updated_at = datetime('now') "
                        "WHERE metric != ?",
                        ("bot_start_time",),
                    )
                cursor.close()
            return True
        except Exception as e:
            logger.error(f"Error resetting statistics: {e}")
            return False

    # ==================== PENDING APPROVALS ====================

    def add_pending_approval(self, media_type: str, file_id: str, chat_id: int, user_id: int) -> int:
        """Add a pending approval and return its ID"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if self.use_postgres:
                cursor.execute("""
                    INSERT INTO pending_approvals (media_type, file_id, chat_id, user_id)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id
                """, (media_type, file_id, chat_id, user_id))
                result = cursor.fetchone()
                approval_id = result[0] if result else None
            else:
                cursor.execute("""
                    INSERT INTO pending_approvals (media_type, file_id, chat_id, user_id)
                    VALUES (?, ?, ?, ?)
                """, (media_type, file_id, chat_id, user_id))
                approval_id = cursor.lastrowid
            cursor.close()
            return approval_id

    def get_pending_approval(self, approval_id: int) -> Optional[Dict]:
        """Get pending approval by ID"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if self.use_postgres:
                cursor.execute("""
                    SELECT id, media_type, file_id, chat_id, user_id, status, created_at
                    FROM pending_approvals WHERE id = %s AND status = 'pending'
                """, (approval_id,))
            else:
                cursor.execute("""
                    SELECT id, media_type, file_id, chat_id, user_id, status, created_at
                    FROM pending_approvals WHERE id = ? AND status = 'pending'
                """, (approval_id,))

            columns = [desc[0] for desc in cursor.description]
            result = cursor.fetchone()
            cursor.close()

            return dict(zip(columns, result)) if result else None

    def resolve_approval(self, approval_id: int, status: str):
        """Resolve a pending approval"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if self.use_postgres:
                cursor.execute("""
                    UPDATE pending_approvals SET status = %s WHERE id = %s
                """, (status, approval_id))
            else:
                cursor.execute("""
                    UPDATE pending_approvals SET status = ? WHERE id = ?
                """, (status, approval_id))
            cursor.close()

    def get_pending_count(self) -> int:
        """Get count of pending approvals"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM pending_approvals WHERE status = 'pending'")
            result = cursor.fetchone()
            cursor.close()
            return result[0] if result else 0

    # ==================== QUEUE, SEARCH, FAVORITES ====================

    def enqueue_video(self, chat_id: int, user_id: int, video: Dict[str, Any]) -> bool:
        """Persist a queued video so it survives restarts/deploys."""
        try:
            with self.get_connection() as conn:
                cur = conn.cursor()
                values = (chat_id, user_id, video["file_id"], video["file_unique_id"],
                          int(video.get("file_size", 0)), int(video.get("duration", 0)),
                          int(video.get("width", 0)), int(video.get("height", 0)), video.get("checksum"))
                if self.use_postgres:
                    cur.execute("""INSERT INTO queue_items
                        (chat_id,user_id,file_id,file_unique_id,file_size,duration,width,height,checksum)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""", values)
                else:
                    cur.execute("""INSERT INTO queue_items
                        (chat_id,user_id,file_id,file_unique_id,file_size,duration,width,height,checksum)
                        VALUES (?,?,?,?,?,?,?,?,?)""", values)
                cur.close()
            return True
        except Exception as e:
            logger.error("Could not persist queue item: %s", e)
            return False

    def get_queue(self, chat_id: int, user_id: int) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = conn.cursor()
            if self.use_postgres:
                cur.execute("SELECT id,file_id,file_unique_id,file_size,duration,width,height,checksum,added_at FROM queue_items WHERE chat_id=%s AND user_id=%s ORDER BY id", (chat_id,user_id))
            else:
                cur.execute("SELECT id,file_id,file_unique_id,file_size,duration,width,height,checksum,added_at FROM queue_items WHERE chat_id=? AND user_id=? ORDER BY id", (chat_id,user_id))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            cur.close()
            return [dict(zip(cols, row)) for row in rows]

    def clear_queue(self, chat_id: int, user_id: int) -> int:
        with self.get_connection() as conn:
            cur = conn.cursor()
            if self.use_postgres:
                cur.execute("DELETE FROM queue_items WHERE chat_id=%s AND user_id=%s", (chat_id,user_id))
            else:
                cur.execute("DELETE FROM queue_items WHERE chat_id=? AND user_id=?", (chat_id,user_id))
            count = cur.rowcount
            cur.close()
            return count

    def remove_queue_items(self, chat_id: int, user_id: int, unique_ids: List[str]) -> None:
        if not unique_ids:
            return
        with self.get_connection() as conn:
            cur = conn.cursor()
            for uid in unique_ids:
                if self.use_postgres:
                    cur.execute("DELETE FROM queue_items WHERE chat_id=%s AND user_id=%s AND file_unique_id=%s", (chat_id,user_id,uid))
                else:
                    cur.execute("DELETE FROM queue_items WHERE chat_id=? AND user_id=? AND file_unique_id=?", (chat_id,user_id,uid))
            cur.close()

    def get_expired_queues(self, timeout_seconds: int) -> List[Dict[str, Any]]:
        """Return queue owners whose oldest/latest queue item has timed out."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            if self.use_postgres:
                cur.execute("""SELECT chat_id,user_id,MAX(added_at) AS last_added FROM queue_items
                    GROUP BY chat_id,user_id HAVING MAX(added_at) < CURRENT_TIMESTAMP - (%s * INTERVAL '1 second')""", (timeout_seconds,))
            else:
                cur.execute("""SELECT chat_id,user_id,MAX(added_at) AS last_added FROM queue_items
                    GROUP BY chat_id,user_id HAVING (julianday('now') - julianday(MAX(added_at))) * 86400 > ?""", (timeout_seconds,))
            rows = cur.fetchall(); cur.close()
            return [{"chat_id": r[0], "user_id": r[1]} for r in rows]



def search_saved_files(self, user_id: int, query: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Search shared videos, photos, and documents by ID, size, duration, or filename."""
    import re

    raw_query = query.strip()
    q = f"%{raw_query}%"
    out: List[Dict[str, Any]] = []

    # Convert human-readable sizes to bytes.
    # A decimal number without a unit, such as 193.1, is treated as MB.
    # An integer without a unit, such as 193100000, is treated as bytes.
    size_range = None
    match = re.fullmatch(
        r"(\d+(?:\.\d+)?)\s*(bytes?|b|kb|kib|mb|mib|gb|gib)?",
        raw_query.lower(),
    )

    if match:
        number = float(match.group(1))
        unit = match.group(2)

        if unit is None:
            target = number * (1024 ** 2) if "." in match.group(1) else number
        else:
            multipliers = {
                "b": 1,
                "byte": 1,
                "bytes": 1,
                "kb": 1024,
                "kib": 1024,
                "mb": 1024 ** 2,
                "mib": 1024 ** 2,
                "gb": 1024 ** 3,
                "gib": 1024 ** 3,
            }
            target = number * multipliers[unit]

        # Allow a small tolerance for decimal/binary size differences.
        tolerance = max(4096, target * 0.05)
        size_range = (
            max(0, int(target - tolerance)),
            int(target + tolerance),
        )

    with self.get_connection() as conn:
        cur = conn.cursor()

        # Search shared videos by ID, duration, or size.
        video_conditions = [
            "file_unique_id ILIKE %s" if self.use_postgres
            else "file_unique_id LIKE ?",
            "file_id ILIKE %s" if self.use_postgres
            else "file_id LIKE ?",
            "CAST(duration AS TEXT) LIKE ?",
            "CAST(file_size AS TEXT) LIKE ?",
        ]
        video_params = [q, q, q, q]

        if size_range:
            video_conditions.append("file_size BETWEEN ? AND ?")
            video_params.extend(size_range)

        if self.use_postgres:
            video_conditions = [
                c.replace(" LIKE ?", " ILIKE %s")
                for c in video_conditions
            ]
            video_conditions = [
                c.replace(" BETWEEN ? AND ?", " BETWEEN %s AND %s")
                for c in video_conditions
            ]

        video_params.append(limit)
        placeholder = "%s" if self.use_postgres else "?"

        cur.execute(
            "SELECT file_id, file_unique_id, file_size, duration, "
            "width, height, created_at, status FROM videos WHERE "
            + " OR ".join(video_conditions)
            + f" ORDER BY created_at DESC LIMIT {placeholder}",
            tuple(video_params),
        )

        for r in cur.fetchall():
            out.append({
                "type": "video",
                "file_id": r[0],
                "unique_id": r[1],
                "file_size": r[2] or 0,
                "duration": r[3] or 0,
                "width": r[4] or 0,
                "height": r[5] or 0,
                "label": "Video",
                "created_at": r[6],
                "status": r[7],
            })

        # Search shared photos by ID.
        if self.use_postgres:
            cur.execute("""
                SELECT file_id, file_unique_id, created_at, status
                FROM photos
                WHERE file_unique_id ILIKE %s OR file_id ILIKE %s
                ORDER BY created_at DESC LIMIT %s
            """, (q, q, limit))
        else:
            cur.execute("""
                SELECT file_id, file_unique_id, created_at, status
                FROM photos
                WHERE file_unique_id LIKE ? OR file_id LIKE ?
                ORDER BY created_at DESC LIMIT ?
            """, (q, q, limit))

        for r in cur.fetchall():
            out.append({
                "type": "photo",
                "file_id": r[0],
                "unique_id": r[1],
                "file_size": 0,
                "duration": 0,
                "width": 0,
                "height": 0,
                "label": "Photo",
                "created_at": r[2],
                "status": r[3],
            })

        # Search documents by filename, ID, or size.
        doc_conditions = [
            "COALESCE(file_name, '') ILIKE %s" if self.use_postgres
            else "COALESCE(file_name, '') LIKE ?",
            "file_unique_id ILIKE %s" if self.use_postgres
            else "file_unique_id LIKE ?",
            "file_id ILIKE %s" if self.use_postgres
            else "file_id LIKE ?",
            "CAST(file_size AS TEXT) LIKE ?",
        ]
        doc_params = [q, q, q, q]

        if size_range:
            doc_conditions.append("file_size BETWEEN ? AND ?")
            doc_params.extend(size_range)

        if self.use_postgres:
            doc_conditions = [
                c.replace(" LIKE ?", " ILIKE %s")
                for c in doc_conditions
            ]
            doc_conditions = [
                c.replace(" BETWEEN ? AND ?", " BETWEEN %s AND %s")
                for c in doc_conditions
            ]

        doc_params.append(limit)

        cur.execute(
            "SELECT file_id, file_unique_id, file_name, file_size, "
            "created_at, status FROM documents WHERE "
            + " OR ".join(doc_conditions)
            + f" ORDER BY created_at DESC LIMIT {placeholder}",
            tuple(doc_params),
        )

        for r in cur.fetchall():
            out.append({
                "type": "document",
                "file_id": r[0],
                "unique_id": r[1],
                "file_size": r[3] or 0,
                "duration": 0,
                "width": 0,
                "height": 0,
                "label": r[2] or "Document",
                "created_at": r[4],
                "status": r[5],
            })

        cur.close()

    out.sort(
        key=lambda x: str(x.get("created_at") or ""),
        reverse=True,
    )
    return out[:limit]


    def get_media_by_unique_id(self, media_type: str, file_unique_id: str) -> Optional[Dict[str, Any]]:
        tables = {"video": ("videos", "file_id,file_unique_id,file_size,duration,width,height,status"),
                  "photo": ("photos", "file_id,file_unique_id,status"),
                  "document": ("documents", "file_id,file_unique_id,file_name,file_size,status")}
        if media_type not in tables: return None
        table, columns = tables[media_type]
        with self.get_connection() as conn:
            cur = conn.cursor()
            if self.use_postgres: cur.execute(f"SELECT {columns} FROM {table} WHERE file_unique_id=%s", (file_unique_id,))
            else: cur.execute(f"SELECT {columns} FROM {table} WHERE file_unique_id=?", (file_unique_id,))
            r = cur.fetchone(); cur.close()
            if not r: return None
            if media_type == "video": return {"type":media_type,"file_id":r[0],"file_unique_id":r[1],"file_size":r[2] or 0,"duration":r[3] or 0,"width":r[4] or 0,"height":r[5] or 0,"status":r[6]}
            if media_type == "photo": return {"type":media_type,"file_id":r[0],"file_unique_id":r[1],"status":r[2]}
            return {"type":media_type,"file_id":r[0],"file_unique_id":r[1],"label":r[2] or "Document","file_size":r[3] or 0,"status":r[4]}


    def get_all_saved_videos(self, limit: int = 100000) -> List[Dict[str, Any]]:
        """Return saved videos from the shared library, including previously sent videos."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT file_id, file_unique_id, file_size, duration, "
                "width, height, checksum FROM videos ORDER BY id LIMIT "
                + ("%s" if self.use_postgres else "?"),
                (limit,),
            )
            rows = cur.fetchall()
            columns = [col[0] for col in cur.description]
            cur.close()
            return [dict(zip(columns, row)) for row in rows]
    def add_skipped_video(
        self, item: Dict[str, Any], reason: str, source: str = "library"
    ) -> None:
        """Save or update a video that failed during bulk sending."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            p = "%s" if self.use_postgres else "?"

            if self.use_postgres:
                cur.execute("""
                    INSERT INTO skipped_videos
                        (file_unique_id, file_id, reason, source, attempts)
                    VALUES (%s, %s, %s, %s, 1)
                    ON CONFLICT (file_unique_id) DO UPDATE SET
                        file_id = EXCLUDED.file_id,
                        reason = EXCLUDED.reason,
                        source = EXCLUDED.source,
                        attempts = skipped_videos.attempts + 1,
                        last_seen = CURRENT_TIMESTAMP
                """, (
                    item["file_unique_id"], item["file_id"],
                    str(reason)[:1000], source
                ))
            else:
                cur.execute("""
                    INSERT INTO skipped_videos
                        (file_unique_id, file_id, reason, source, attempts)
                    VALUES (?, ?, ?, ?, 1)
                    ON CONFLICT(file_unique_id) DO UPDATE SET
                        file_id = excluded.file_id,
                        reason = excluded.reason,
                        source = excluded.source,
                        attempts = skipped_videos.attempts + 1,
                        last_seen = CURRENT_TIMESTAMP
                """, (
                    item["file_unique_id"], item["file_id"],
                    str(reason)[:1000], source
                ))
            cur.close()

    def get_skipped_videos(
        self, limit: int = 8, offset: int = 0
    ) -> List[Dict[str, Any]]:
        """Return a page of skipped videos."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            p = "%s" if self.use_postgres else "?"
            cur.execute(
                "SELECT file_unique_id, file_id, reason, source, "
                "attempts, last_seen FROM skipped_videos "
                f"ORDER BY last_seen DESC LIMIT {p} OFFSET {p}",
                (limit, offset),
            )
            columns = [col[0] for col in cur.description]
            rows = cur.fetchall()
            cur.close()
            return [dict(zip(columns, row)) for row in rows]

    def get_skipped_video_count(self) -> int:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM skipped_videos")
            count = cur.fetchone()[0]
            cur.close()
            return int(count)

    def remove_skipped_video(self, file_unique_id: str) -> bool:
        """Remove from the skipped list only; preserve the saved video."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            p = "%s" if self.use_postgres else "?"
            cur.execute(
                f"DELETE FROM skipped_videos WHERE file_unique_id = {p}",
                (file_unique_id,),
            )
            removed = cur.rowcount > 0
            cur.close()
            return removed

    def clear_skipped_videos(self) -> int:
        """Clear the skipped list without deleting saved videos."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM skipped_videos")
            count = cur.rowcount
            cur.close()
            return max(0, count)
    def delete_saved_video(self, file_unique_id: str) -> bool:
        """Delete a video from the shared library, all queues, and all favorites."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            p = "%s" if self.use_postgres else "?"

            cur.execute(
                f"DELETE FROM queue_items WHERE file_unique_id = {p}",
                (file_unique_id,),
            )
            cur.execute(
                f"DELETE FROM favorites WHERE file_unique_id = {p}",
                (file_unique_id,),
            )
            cur.execute(
                f"DELETE FROM videos WHERE file_unique_id = {p}",
                (file_unique_id,),
            )
            deleted = cur.rowcount > 0
            cur.close()
            return deleted

    def get_favorite_count(self, user_id: int) -> int:
        with self.get_connection() as conn:
            cur = conn.cursor()
            if self.use_postgres: cur.execute("SELECT COUNT(*) FROM favorites WHERE user_id=%s", (user_id,))
            else: cur.execute("SELECT COUNT(*) FROM favorites WHERE user_id=?", (user_id,))
            result = cur.fetchone(); cur.close()
            return int(result[0] if result else 0)

    def toggle_favorite(self, user_id: int, media_type: str, file_unique_id: str, file_id: str, label: str = "") -> bool:
        """Toggle favorite; returns True if favorited, False if removed."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            if self.use_postgres:
                cur.execute("SELECT id FROM favorites WHERE user_id=%s AND media_type=%s AND file_unique_id=%s", (user_id,media_type,file_unique_id))
            else:
                cur.execute("SELECT id FROM favorites WHERE user_id=? AND media_type=? AND file_unique_id=?", (user_id,media_type,file_unique_id))
            found = cur.fetchone()
            if found:
                if self.use_postgres: cur.execute("DELETE FROM favorites WHERE id=%s", (found[0],))
                else: cur.execute("DELETE FROM favorites WHERE id=?", (found[0],))
                cur.close(); return False
            if self.use_postgres:
                cur.execute("INSERT INTO favorites (user_id,media_type,file_unique_id,file_id,label) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (user_id,media_type,file_unique_id) DO NOTHING", (user_id,media_type,file_unique_id,file_id,label))
            else:
                cur.execute("INSERT OR IGNORE INTO favorites (user_id,media_type,file_unique_id,file_id,label) VALUES (?,?,?,?,?)", (user_id,media_type,file_unique_id,file_id,label))
            cur.close(); return True

    def get_video_by_unique_id(self, file_unique_id: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = conn.cursor()
            if self.use_postgres:
                cur.execute("SELECT file_id,file_unique_id,file_size,duration,width,height,status FROM videos WHERE file_unique_id=%s", (file_unique_id,))
            else:
                cur.execute("SELECT file_id,file_unique_id,file_size,duration,width,height,status FROM videos WHERE file_unique_id=?", (file_unique_id,))
            r = cur.fetchone(); cur.close()
            if not r: return None
            return {"file_id":r[0],"file_unique_id":r[1],"file_size":r[2] or 0,"duration":r[3] or 0,"width":r[4] or 0,"height":r[5] or 0,"status":r[6]}

    def get_favorites(self, user_id: int, limit: int = 20) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = conn.cursor()
            if self.use_postgres:
                cur.execute("SELECT media_type,file_unique_id,file_id,label,created_at FROM favorites WHERE user_id=%s ORDER BY created_at DESC LIMIT %s", (user_id,limit))
            else:
                cur.execute("SELECT media_type,file_unique_id,file_id,label,created_at FROM favorites WHERE user_id=? ORDER BY created_at DESC LIMIT ?", (user_id,limit))
            rows = cur.fetchall(); cur.close()
            return [{"media_type":r[0],"file_unique_id":r[1],"file_id":r[2],"label":r[3],"created_at":r[4]} for r in rows]

    def get_health_summary(self) -> Dict[str, int]:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM queue_items")
            queued = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM videos")
            videos = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM favorites")
            favorites = cur.fetchone()[0]
            cur.close()
            return {"queued": queued, "videos": videos, "favorites": favorites}

    # ==================== CLEANUP METHODS ====================

    def cleanup_old_records(self, days: int = 30):
        """Clean up old sent/processed records"""
        with self.get_connection() as conn:
                cursor = conn.cursor()
            if self.use_postgres:
                cursor.execute("""
                    DELETE FROM videos WHERE status = 'sent' AND sent_at < CURRENT_TIMESTAMP - INTERVAL '%s days'
                """, (days,))
                cursor.execute("""
                    DELETE FROM pending_approvals WHERE status != 'pending' AND created_at < CURRENT_TIMESTAMP - INTERVAL '%s days'
                """, (days,))
            else:
                cursor.execute("""
                    DELETE FROM videos WHERE status = 'sent' AND sent_at < datetime('now', '-{} days')
                """.format(days))
                cursor.execute("""
                    DELETE FROM pending_approvals WHERE status != 'pending' AND created_at < datetime('now', '-{} days')
                """.format(days))
                cursor.close()

    def close(self):
        """Close database connections"""
        if self.use_postgres and self.connection_pool:
            self.connection_pool.closeall()
        elif self.sqlite_conn:
            self.sqlite_conn.close()


# Global database instance
db = Database()
