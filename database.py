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

            cursor.execute(users_table)
            cursor.execute(settings_table)
            cursor.execute(videos_table)
            cursor.execute(photos_table)
            cursor.execute(stats_table)
            cursor.execute(documents_table)
            cursor.execute(approvals_table)

            # Insert default statistics if not exist
            default_stats = [
                ("total_videos", 0),
                ("total_photos", 0),
                ("total_documents", 0),
                ("total_batches", 0),
                ("rejected_photos", 0),
                ("rejected_documents", 0),
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
                cursor.close()
                return True
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
                cursor.close()
                return True
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
                cursor.close()
                return True
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
