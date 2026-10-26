"""
Enhanced Telegram Bot with PostgreSQL Database
Features:
- PostgreSQL/SQLite database storage
- Admin ID management via database
- Strip sender name/captions by default
- Photo accept/ignore options
- Document accept/ignore options
- Persistent settings
"""

import os
import sys
import logging
import asyncio
import random
import time
import json
import hashlib
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Set, Tuple, Any
from dataclasses import dataclass, asdict
from functools import wraps

from telegram import Update, InputMediaVideo, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters, CallbackQueryHandler
)
from telegram.error import TelegramError, RetryAfter, TimedOut

# Add current directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import database module
try:
    from database import Database, db
    DATABASE_ENABLED = True
except ImportError:
    DATABASE_ENABLED = False
    print("Warning: database module not found, running without database")

# -----------------------------
# Logging with Rotation
# -----------------------------
from logging.handlers import RotatingFileHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        RotatingFileHandler("bot.log", maxBytes=5*1024*1024, backupCount=3),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# -----------------------------
# Default Admin IDs (used if database is empty)
# -----------------------------
DEFAULT_ADMIN_IDS = [
    8976017144,  # KR
]

# -----------------------------
# Configuration Keys
# -----------------------------
class ConfigKeys:
    PHOTO_APPROVAL_MODE = "photo_approval_mode"
    DOCUMENT_APPROVAL_MODE = "document_approval_mode"
    ACCEPT_PHOTOS = "accept_photos"
    ACCEPT_DOCUMENTS = "accept_documents"
    STRIP_METADATA = "strip_metadata"
    AUTO_CAPTION_MODE = "auto_caption_mode"
    CAPTION_TEMPLATE = "caption_template"
    COMPRESSION_MODE = "compression_mode"
    FLUSH_TIMEOUT = "flush_timeout"
    ALBUM_SIZE = "album_size"
    BATCH_COUNT = "batch_count"
    LAST_VIDEO_TIME = "last_video_time"

# -----------------------------
# Default Settings
# -----------------------------
DEFAULT_SETTINGS = {
    ConfigKeys.PHOTO_APPROVAL_MODE: False,
    ConfigKeys.DOCUMENT_APPROVAL_MODE: False,
    ConfigKeys.ACCEPT_PHOTOS: True,
    ConfigKeys.ACCEPT_DOCUMENTS: False,
    ConfigKeys.STRIP_METADATA: True,  # Strip sender name and captions by default
    ConfigKeys.AUTO_CAPTION_MODE: False,
    ConfigKeys.CAPTION_TEMPLATE: "",
    ConfigKeys.COMPRESSION_MODE: False,
    ConfigKeys.FLUSH_TIMEOUT: 60,
    ConfigKeys.ALBUM_SIZE: 10,
    ConfigKeys.BATCH_COUNT: 0,
    ConfigKeys.LAST_VIDEO_TIME: 0,
}

# -----------------------------
# Data Classes
# -----------------------------
@dataclass
class VideoInfo:
    file_id: str
    file_unique_id: str
    file_size: int
    duration: int
    width: int
    height: int
    timestamp: float
    checksum: str

@dataclass
class PhotoInfo:
    file_id: str
    file_unique_id: str
    timestamp: float

@dataclass
class DocumentInfo:
    file_id: str
    file_unique_id: str
    file_name: str
    file_size: int
    mime_type: str
    timestamp: float

# -----------------------------
# Settings Manager (Persistent)
# -----------------------------
class SettingsManager:
    """Manages bot settings with database persistence"""

    def __init__(self):
        self._cache: Dict[str, Any] = {}
        self._load_settings()

    def _load_settings(self):
        """Load settings from database or defaults"""
        if DATABASE_ENABLED:
            try:
                stored = db.get_all_settings()
                self._cache = {**DEFAULT_SETTINGS, **stored}
                logger.info(f"Loaded {len(stored)} settings from database")
            except Exception as e:
                logger.error(f"Error loading settings: {e}")
                self._cache = DEFAULT_SETTINGS.copy()
        else:
            # Try to load from JSON file
            try:
                if os.path.exists("bot_settings.json"):
                    with open("bot_settings.json", 'r') as f:
                        stored = json.load(f)
                    self._cache = {**DEFAULT_SETTINGS, **stored}
                    logger.info("Settings loaded from JSON file")
                else:
                    self._cache = DEFAULT_SETTINGS.copy()
            except Exception as e:
                logger.error(f"Error loading settings: {e}")
                self._cache = DEFAULT_SETTINGS.copy()

    def get(self, key: str, default: Any = None) -> Any:
        """Get a setting value"""
        return self._cache.get(key, default)

    def set(self, key: str, value: Any):
        """Set and persist a setting"""
        self._cache[key] = value

        if DATABASE_ENABLED:
            try:
                db.set_setting(key, value)
            except Exception as e:
                logger.error(f"Error saving setting to database: {e}")

        # Always save to JSON as backup
        try:
            with open("bot_settings.json", 'w') as f:
                json.dump(self._cache, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving settings to JSON: {e}")

    def toggle(self, key: str) -> bool:
        """Toggle a boolean setting"""
        current = self.get(key, False)
        new_value = not current
        self.set(key, new_value)
        return new_value

    def get_all(self) -> Dict[str, Any]:
        """Get all settings"""
        return self._cache.copy()

    def reset_to_defaults(self):
        """Reset all settings to defaults"""
        self._cache = DEFAULT_SETTINGS.copy()
        if DATABASE_ENABLED:
            for key, value in DEFAULT_SETTINGS.items():
                db.set_setting(key, value)
        with open("bot_settings.json", 'w') as f:
            json.dump(self._cache, f, indent=2)

# Initialize settings manager
settings = SettingsManager()

# -----------------------------
# Admin Manager
# -----------------------------
class AdminManager:
    """Manages admin users with database persistence"""

    def __init__(self):
        self._default_admins = DEFAULT_ADMIN_IDS.copy()

    def is_admin(self, user_id: int) -> bool:
        """Check if user is admin"""
        if DATABASE_ENABLED:
            try:
                # Check database first
                if db.is_admin(user_id):
                    return True
            except Exception as e:
                logger.error(f"Error checking admin status: {e}")

        # Fallback to default admins
        return user_id in self._default_admins

    def add_admin(self, user_id: int, username: str = None, 
                  first_name: str = None, last_name: str = None) -> bool:
        """Add a new admin"""
        if DATABASE_ENABLED:
            try:
                return db.add_admin(user_id, username, first_name, last_name)
            except Exception as e:
                logger.error(f"Error adding admin: {e}")
                return False
        else:
            if user_id not in self._default_admins:
                self._default_admins.append(user_id)
                return True
            return False

    def remove_admin(self, user_id: int) -> bool:
        """Remove an admin"""
        if user_id in self._default_admins:
            self._default_admins.remove(user_id)

        if DATABASE_ENABLED:
            try:
                return db.remove_admin(user_id)
            except Exception as e:
                logger.error(f"Error removing admin: {e}")
                return False
        return True

    def get_all_admins(self) -> List[Dict]:
        """Get all admin users"""
        admins = []

        if DATABASE_ENABLED:
            try:
                db_admins = db.get_all_admins()
                admins.extend(db_admins)
            except Exception as e:
                logger.error(f"Error getting admins: {e}")

        # Add default admins that might not be in DB
        existing_ids = {a.get('user_id') for a in admins}
        for uid in self._default_admins:
            if uid not in existing_ids:
                admins.append({
                    'user_id': uid,
                    'username': None,
                    'first_name': 'Default Admin',
                    'source': 'default'
                })

        return admins

# Initialize admin manager
admin_manager = AdminManager()

# -----------------------------
# Admin-only decorator
# -----------------------------
def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id

        if not admin_manager.is_admin(user_id):
            if update.message:
                await update.message.reply_text("🔒 This bot is private. Access denied.")
            elif update.callback_query:
                await update.callback_query.answer("🔒 Access denied", show_alert=True)
            logger.warning(f"Unauthorized access attempt by {user_id}")
            return

        # Update user activity in database
        if DATABASE_ENABLED:
            try:
                user = update.effective_user
                db.update_user_activity(
                    user_id=user.id,
                    username=user.username,
                    first_name=user.first_name,
                    last_name=user.last_name
                )
            except Exception as e:
                logger.error(f"Error updating user activity: {e}")

        return await func(update, context)
    return wrapper

# -----------------------------
# Utility Functions
# -----------------------------
def generate_checksum(file_size: int, duration: int, width: int, height: int) -> str:
    """Generate a checksum for duplicate detection"""
    content = f"{file_size}_{duration}_{width}x{height}"
    return hashlib.md5(content.encode()).hexdigest()

def format_size(size_bytes: int) -> str:
    """Format bytes to human readable"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024**2:
        return f"{size_bytes/1024:.1f} KB"
    elif size_bytes < 1024**3:
        return f"{size_bytes/1024**2:.1f} MB"
    else:
        return f"{size_bytes/1024**3:.2f} GB"

def format_duration(seconds: int) -> str:
    """Format seconds to MM:SS"""
    mins = seconds // 60
    secs = seconds % 60
    return f"{mins:02d}:{secs:02d}"

def strip_media_metadata(media):
    """Remove sender name and captions from media"""
    return media

# -----------------------------
# Keyboard Markups
# -----------------------------
def get_main_keyboard():
    keyboard = [
        [InlineKeyboardButton("📊 Stats", callback_data="stats"),
         InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
        [InlineKeyboardButton("🖼️ Photos", callback_data="photo_menu"),
         InlineKeyboardButton("📄 Docs", callback_data="doc_menu")],
        [InlineKeyboardButton("🚀 Flush Now", callback_data="flush"),
         InlineKeyboardButton("❓ Help", callback_data="help")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_photo_settings_keyboard():
    accept_photos = settings.get(ConfigKeys.ACCEPT_PHOTOS, True)
    photo_approval = settings.get(ConfigKeys.PHOTO_APPROVAL_MODE, False)

    keyboard = [
        [InlineKeyboardButton(
            f"{'✅' if accept_photos else '❌'} Accept Photos", 
            callback_data="toggle_accept_photos")],
        [InlineKeyboardButton(
            f"{'✅' if photo_approval else '❌'} Approval Mode", 
            callback_data="toggle_photo_approval")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_document_settings_keyboard():
    accept_docs = settings.get(ConfigKeys.ACCEPT_DOCUMENTS, False)
    doc_approval = settings.get(ConfigKeys.DOCUMENT_APPROVAL_MODE, False)

    keyboard = [
        [InlineKeyboardButton(
            f"{'✅' if accept_docs else '❌'} Accept Documents", 
            callback_data="toggle_accept_docs")],
        [InlineKeyboardButton(
            f"{'✅' if doc_approval else '❌'} Approval Mode", 
            callback_data="toggle_doc_approval")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_settings_keyboard():
    strip_meta = settings.get(ConfigKeys.STRIP_METADATA, True)
    auto_caption = settings.get(ConfigKeys.AUTO_CAPTION_MODE, False)
    compression = settings.get(ConfigKeys.COMPRESSION_MODE, False)
    flush_timeout = settings.get(ConfigKeys.FLUSH_TIMEOUT, 60)
    album_size = settings.get(ConfigKeys.ALBUM_SIZE, 10)

    keyboard = [
        [InlineKeyboardButton(
            f"{'✅' if strip_meta else '❌'} Strip Names/Captions", 
            callback_data="toggle_strip_meta")],
        [InlineKeyboardButton(
            f"{'✅' if auto_caption else '❌'} Auto Captions", 
            callback_data="toggle_auto_caption")],
        [InlineKeyboardButton(
            f"{'✅' if compression else '❌'} Compression", 
            callback_data="toggle_compression")],
        [InlineKeyboardButton(f"⏱️ Timeout: {flush_timeout}s", callback_data="adjust_timeout"),
         InlineKeyboardButton(f"📦 Album: {album_size}", callback_data="adjust_album")],
        [InlineKeyboardButton("🔄 Reset Defaults", callback_data="reset_settings"),
         InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_admin_keyboard():
    keyboard = [
        [InlineKeyboardButton("➕ Add Admin", callback_data="add_admin_prompt"),
         InlineKeyboardButton("➖ Remove Admin", callback_data="remove_admin_prompt")],
        [InlineKeyboardButton("📋 List Admins", callback_data="list_admins"),
         InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]
    ]
    return InlineKeyboardMarkup(keyboard)

# -----------------------------
# Commands
# -----------------------------
@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # Get stats
    total_videos = db.get_stat("total_videos") if DATABASE_ENABLED else 0
    total_photos = db.get_stat("total_photos") if DATABASE_ENABLED else 0
    total_batches = db.get_stat("total_batches") if DATABASE_ENABLED else 0

    welcome_text = (
        f"🤖 <b>Bot Ready!</b>\n\n"
        f"👤 Welcome, {user.first_name}!\n"
        f"🆔 Your ID: <code>{user.id}</code>\n\n"
        f"📊 <b>Statistics:</b>\n"
        f"• Videos processed: {total_videos}\n"
        f"• Photos processed: {total_photos}\n"
        f"• Batches sent: {total_batches}\n\n"
        f"Use the menu below or /help for commands:"
    )
    
    await update.message.reply_text(
        text=welcome_text, 
        parse_mode="HTML", 
        reply_markup=get_main_keyboard()
    )


@admin_only
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    strip_status = "ON ✅" if settings.get(ConfigKeys.STRIP_METADATA, True) else "OFF ❌"

    text = f"""📋 <b>Available Commands:</b>

<b>Basic:</b>
/start – Show bot status & menu
/help – Show this help
/id – Get your Telegram ID
/ping – Check bot latency

<b>Admin Management:</b>
/addadmin &lt;user_id&gt; – Add new admin
/removeadmin &lt;user_id&gt; – Remove admin
/listadmins – List all admins

<b>Media Settings:</b>
/toggle_photos – Toggle accept photos
/toggle_documents – Toggle accept documents
/toggle_photo_approval – Toggle photo approval mode
/toggle_doc_approval – Toggle document approval mode
/toggle_strip – Toggle strip names/captions

<b>Configuration:</b>
/settings – Open settings menu
/set_timeout &lt;seconds&gt; – Set batch timeout
/set_album_size &lt;1-10&gt; – Set album size
/toggle_caption – Toggle auto-captions
/set_caption &lt;text&gt; – Set caption template

<b>Media Control:</b>
/approve – Approve pending media
/reject – Reject pending media
/flush – Send pending videos now
/clear – Clear current queue

<b>Info:</b>
/stats – Detailed statistics
/export – Export stats to file
/dbinfo – Database information

<i>Strip metadata (sender/captions): {strip_status}</i>"""
    await update.message.reply_text(text, parse_mode="HTML")


@admin_only
async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🆔 Your Telegram ID: <code>{update.effective_user.id}</code>", parse_mode="HTML")


@admin_only
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_time = time.time()
    msg = await update.message.reply_text("🏓 Pong!")
    latency = (time.time() - start_time) * 1000
    await msg.edit_text(f"🏓 Pong!\n⚡ Latency: {latency:.2f}ms")


# ==================== ADMIN MANAGEMENT COMMANDS ====================

@admin_only
async def add_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a new admin by user ID"""
    if not context.args:
        await update.message.reply_text(
            "Usage: /addadmin &lt;user_id&gt; [username]\n"
            "Example: /addadmin 123456789 johndoe",
            parse_mode="HTML"
        )
        return

    try:
        new_admin_id = int(context.args[0])
        username = context.args[1] if len(context.args) > 1 else None

        if admin_manager.is_admin(new_admin_id):
            await update.message.reply_text(f"⚠️ User {new_admin_id} is already an admin.")
            return

        success = admin_manager.add_admin(new_admin_id, username)

        if success:
            await update.message.reply_text(
                f"✅ Added admin: <code>{new_admin_id}</code>\n"
                f"Username: @{username if username else 'N/A'}",
                parse_mode="HTML"
            )
            logger.info(f"Admin {update.effective_user.id} added new admin {new_admin_id}")
        else:
            await update.message.reply_text("❌ Failed to add admin.")

    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. Must be a number.")


@admin_only
async def remove_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove an admin by user ID"""
    if not context.args:
        await update.message.reply_text(
            "Usage: /removeadmin &lt;user_id&gt;",
            parse_mode="HTML"
        )
        return

    try:
        admin_id = int(context.args[0])

        if admin_id == update.effective_user.id:
            await update.message.reply_text("❌ You cannot remove yourself as admin.")
            return

        if not admin_manager.is_admin(admin_id):
            await update.message.reply_text(f"⚠️ User {admin_id} is not an admin.")
            return

        # Prevent removing default admins
        if admin_id in DEFAULT_ADMIN_IDS:
            await update.message.reply_text("❌ Cannot remove default admin.")
            return

        success = admin_manager.remove_admin(admin_id)

        if success:
            await update.message.reply_text(f"✅ Removed admin: <code>{admin_id}</code>", parse_mode="HTML")
            logger.info(f"Admin {update.effective_user.id} removed admin {admin_id}")
        else:
            await update.message.reply_text("❌ Failed to remove admin.")

    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. Must be a number.")


@admin_only
async def list_admins_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all admin users"""
    admins = admin_manager.get_all_admins()

    if not admins:
        await update.message.reply_text("No admins found.")
        return

    text = "👥 <b>Admin Users:</b>\n\n"

    for i, admin in enumerate(admins, 1):
        user_id = admin.get('user_id', 'Unknown')
        username = admin.get('username', 'N/A')
        first_name = admin.get('first_name', 'N/A')
        source = admin.get('source', 'database' if DATABASE_ENABLED else 'default')

        text += f"{i}. <code>{user_id}</code>\n"
        text += f"   Name: {first_name}\n"
        if username:
            text += f"   Username: @{username}\n"
        text += f"   Source: {source}\n\n"

    await update.message.reply_text(text, parse_mode="HTML")


# ==================== MEDIA SETTINGS COMMANDS ====================

@admin_only
async def toggle_photos_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle accepting photos"""
    new_value = settings.toggle(ConfigKeys.ACCEPT_PHOTOS)
    status = "ON ✅" if new_value else "OFF ❌"
    await update.message.reply_text(f"🖼️ Accept photos is now {status}")


@admin_only
async def toggle_documents_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle accepting documents"""
    new_value = settings.toggle(ConfigKeys.ACCEPT_DOCUMENTS)
    status = "ON ✅" if new_value else "OFF ❌"
    await update.message.reply_text(f"📄 Accept documents is now {status}")


@admin_only
async def toggle_photo_approval_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle photo approval mode"""
    new_value = settings.toggle(ConfigKeys.PHOTO_APPROVAL_MODE)
    status = "ON ✅" if new_value else "OFF ❌"
    await update.message.reply_text(f"🖼️ Photo approval mode is now {status}")


@admin_only
async def toggle_doc_approval_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle document approval mode"""
    new_value = settings.toggle(ConfigKeys.DOCUMENT_APPROVAL_MODE)
    status = "ON ✅" if new_value else "OFF ❌"
    await update.message.reply_text(f"📄 Document approval mode is now {status}")


@admin_only
async def toggle_strip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle stripping sender name and captions"""
    new_value = settings.toggle(ConfigKeys.STRIP_METADATA)
    status = "ON ✅" if new_value else "OFF ❌"

    explanation = (
        f"📝 Strip metadata is now {status}\n\n"
        f"When ON:\n"
        f"• Removes sender name from forwarded media\n"
        f"• Removes original captions\n"
        f"• Sends clean media only"
    )
    await update.message.reply_text(explanation)


@admin_only
async def toggle_caption_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle auto-caption mode"""
    new_value = settings.toggle(ConfigKeys.AUTO_CAPTION_MODE)
    status = "ON ✅" if new_value else "OFF ❌"

    if new_value:
        await update.message.reply_text(
            f"📝 Auto-caption mode is now {status}\n\n"
            f"Send /set_caption &lt;your text&gt; to set the caption template\n"
            f"Variables: {'{index}'}, {'{total}'}, {'{duration}'}, {'{size}'}",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text(f"📝 Auto-caption mode is now {status}")


@admin_only
async def set_caption_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set caption template"""
    if not context.args:
        current = settings.get(ConfigKeys.CAPTION_TEMPLATE, "")
        await update.message.reply_text(
            f"Current caption template:\n<code>{current}</code>\n\n"
            f"Usage: /set_caption Your caption here\n"
            f"Variables: {'{index}'}, {'{total}'}, {'{duration}'}, {'{size}'}",
            parse_mode="HTML"
        )
        return

    caption = " ".join(context.args)
    settings.set(ConfigKeys.CAPTION_TEMPLATE, caption)
    await update.message.reply_text(f"✅ Caption template set:\n\n{caption}")


@admin_only
async def set_timeout_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set batch flush timeout"""
    if not context.args:
        current = settings.get(ConfigKeys.FLUSH_TIMEOUT, 60)
        await update.message.reply_text(f"Current timeout: {current}s\nUsage: /set_timeout 120")
        return

    try:
        new_timeout = int(context.args[0])
        if new_timeout < 10 or new_timeout > 3600:
            await update.message.reply_text("❌ Timeout must be between 10 and 3600 seconds")
            return

        settings.set(ConfigKeys.FLUSH_TIMEOUT, new_timeout)
        await update.message.reply_text(f"✅ Batch timeout set to {new_timeout} seconds")
    except ValueError:
        await update.message.reply_text("❌ Invalid number")


@admin_only
async def set_album_size_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set album size"""
    if not context.args:
        current = settings.get(ConfigKeys.ALBUM_SIZE, 10)
        await update.message.reply_text(f"Current album size: {current}\nUsage: /set_album_size 5")
        return

    try:
        new_size = int(context.args[0])
        if new_size < 1 or new_size > 10:
            await update.message.reply_text("❌ Album size must be between 1 and 10")
            return

        settings.set(ConfigKeys.ALBUM_SIZE, new_size)
        await update.message.reply_text(f"✅ Album size set to {new_size}")
    except ValueError:
        await update.message.reply_text("❌ Invalid number")


@admin_only
async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show settings menu"""
    await show_settings_menu(update, context)


async def show_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display settings menu"""
    strip_meta = settings.get(ConfigKeys.STRIP_METADATA, True)
    accept_photos = settings.get(ConfigKeys.ACCEPT_PHOTOS, True)
    accept_docs = settings.get(ConfigKeys.ACCEPT_DOCUMENTS, False)
    photo_approval = settings.get(ConfigKeys.PHOTO_APPROVAL_MODE, False)
    doc_approval = settings.get(ConfigKeys.DOCUMENT_APPROVAL_MODE, False)
    auto_caption = settings.get(ConfigKeys.AUTO_CAPTION_MODE, False)
    flush_timeout = settings.get(ConfigKeys.FLUSH_TIMEOUT, 60)
    album_size = settings.get(ConfigKeys.ALBUM_SIZE, 10)

    text = (
        f"⚙️ <b>Bot Settings</b>\n\n"
        f"<b>Media Handling:</b>\n"
        f"• Strip names/captions: {'ON ✅' if strip_meta else 'OFF ❌'}\n"
        f"• Accept photos: {'ON ✅' if accept_photos else 'OFF ❌'}\n"
        f"• Accept documents: {'ON ✅' if accept_docs else 'OFF ❌'}\n\n"
        f"<b>Approval Modes:</b>\n"
        f"• Photo approval: {'ON ✅' if photo_approval else 'OFF ❌'}\n"
        f"• Document approval: {'ON ✅' if doc_approval else 'OFF ❌'}\n\n"
        f"<b>Other Settings:</b>\n"
        f"• Auto captions: {'ON ✅' if auto_caption else 'OFF ❌'}\n"
        f"• Batch timeout: {flush_timeout}s\n"
        f"• Album size: {album_size}\n\n"
        f"<i>All settings are automatically saved and persist across restarts.</i>"
    )

    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=get_settings_keyboard())
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=get_settings_keyboard())


# ==================== MEDIA HANDLING COMMANDS ====================

@admin_only
async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Approve pending media"""
    pending_photo = context.user_data.get("pending_photo")
    pending_doc = context.user_data.get("pending_document")

    if pending_photo:
        await update.message.reply_photo(pending_photo["file_id"])
        context.user_data["pending_photo"] = None
        if DATABASE_ENABLED:
            db.increment_stat("total_photos")
        await update.message.reply_text("✅ Photo approved and sent.")

    elif pending_doc:
        doc = pending_doc
        await update.message.reply_document(
            document=doc["file_id"],
            filename=doc.get("file_name")
        )
        context.user_data["pending_document"] = None
        if DATABASE_ENABLED:
            db.increment_stat("total_documents")
        await update.message.reply_text("✅ Document approved and sent.")

    else:
        await update.message.reply_text("ℹ️ No pending media to approve.")


@admin_only
async def reject_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reject pending media"""
    if context.user_data.get("pending_photo"):
        context.user_data["pending_photo"] = None
        if DATABASE_ENABLED:
            db.increment_stat("rejected_photos")
        await update.message.reply_text("❌ Photo rejected.")

    elif context.user_data.get("pending_document"):
        context.user_data["pending_document"] = None
        if DATABASE_ENABLED:
            db.increment_stat("rejected_documents")
        await update.message.reply_text("❌ Document rejected.")

    else:
        await update.message.reply_text("ℹ️ No pending media to reject.")


@admin_only
async def flush_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send pending videos now"""
    album = context.user_data.get("album", [])
    if not album:
        await update.message.reply_text("ℹ️ No pending videos.")
        return

    await send_album(update, context)
    await update.message.reply_text("✅ Flushed remaining videos.")


@admin_only
async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear current queue"""
    cleared = 0
    if "album" in context.user_data:
        cleared += len(context.user_data["album"])
        context.user_data["album"] = []

    if context.user_data.get("pending_photo"):
        context.user_data["pending_photo"] = None
        cleared += 1

    if context.user_data.get("pending_document"):
        context.user_data["pending_document"] = None
        cleared += 1

    await update.message.reply_text(f"🗑️ Cleared {cleared} items from queue.")


@admin_only
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show detailed statistics"""
    if DATABASE_ENABLED:
        stats = db.get_all_stats()
        total_videos = stats.get("total_videos", 0)
        total_photos = stats.get("total_photos", 0)
        total_docs = stats.get("total_documents", 0)
        total_batches = stats.get("total_batches", 0)
        rejected_photos = stats.get("rejected_photos", 0)
        rejected_docs = stats.get("rejected_documents", 0)
        bot_start = stats.get("bot_start_time", time.time())
    else:
        total_videos = settings.get("total_videos", 0)
        total_photos = settings.get("total_photos", 0)
        total_docs = 0
        total_batches = settings.get("total_batches", 0)
        rejected_photos = 0
        rejected_docs = 0
        bot_start = time.time()

    uptime = timedelta(seconds=int(time.time() - bot_start))
    pending_count = db.get_pending_count() if DATABASE_ENABLED else 0

    text = (
        f"📊 <b>Bot Statistics</b>\n\n"
        f"<b>Media Processed:</b>\n"
        f"• Videos: {total_videos}\n"
        f"• Photos: {total_photos}\n"
        f"• Documents: {total_docs}\n\n"
        f"<b>Batches:</b>\n"
        f"• Total batches: {total_batches}\n\n"
        f"<b>Rejections:</b>\n"
        f"• Photos rejected: {rejected_photos}\n"
        f"• Documents rejected: {rejected_docs}\n\n"
        f"<b>System:</b>\n"
        f"• Uptime: {uptime}\n"
        f"• Pending approvals: {pending_count}\n"
        f"• Database: {'PostgreSQL ✅' if DATABASE_ENABLED and db.use_postgres else 'SQLite' if DATABASE_ENABLED else 'Disabled'}"
    )

    await update.message.reply_text(text, parse_mode="HTML")


@admin_only
async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Export statistics to file"""
    try:
        export_data = {
            "export_time": datetime.now().isoformat(),
            "settings": settings.get_all(),
            "admins": admin_manager.get_all_admins(),
            "database_stats": db.get_all_stats() if DATABASE_ENABLED else {},
        }

        filename = f"bot_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(filename, 'w') as f:
            json.dump(export_data, f, indent=2, default=str)

        await update.message.reply_document(
            document=open(filename, 'rb'),
            caption="📊 Bot export file"
        )
        os.remove(filename)
    except Exception as e:
        await update.message.reply_text(f"❌ Export failed: {str(e)}")


@admin_only
async def db_info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show database information"""
    if not DATABASE_ENABLED:
        await update.message.reply_text("❌ Database is not enabled.")
        return

    db_type = "PostgreSQL" if db.use_postgres else "SQLite"

    with db.get_connection() as conn:
        cursor = conn.cursor()

        tables = ["users", "videos", "photos", "documents", "settings", "statistics", "pending_approvals"]
        counts = {}

        for table in tables:
            try:
                cursor.execute(f"SELECT COUNT(*) FROM {table}")
                counts[table] = cursor.fetchone()[0]
            except:
                counts[table] = "Error"

        cursor.close()

    text = (
        f"🗄️ <b>Database Information</b>\n\n"
        f"<b>Type:</b> {db_type}\n"
        f"<b>Status:</b> Connected ✅\n\n"
        f"<b>Table Counts:</b>\n"
    )

    for table, count in counts.items():
        text += f"• {table}: {count}\n"

    await update.message.reply_text(text, parse_mode="HTML")


# ==================== MEDIA HANDLERS ====================

@admin_only
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming videos"""
    message = update.message

    if DATABASE_ENABLED:
        db.set_setting(ConfigKeys.LAST_VIDEO_TIME, time.time())

    try:
        await message.delete()
    except Exception as e:
        logger.warning(f"Could not delete message: {e}")

    if not message.video:
        return

    video = message.video

    video_info = VideoInfo(
        file_id=video.file_id,
        file_unique_id=video.file_unique_id,
        file_size=video.file_size or 0,
        duration=video.duration or 0,
        width=video.width or 0,
        height=video.height or 0,
        timestamp=time.time(),
        checksum=generate_checksum(video.file_size or 0, video.duration or 0, video.width or 0, video.height or 0)
    )

    if DATABASE_ENABLED:
        if db.video_exists(file_unique_id=video.file_unique_id):
            logger.info(f"Duplicate video (ID): {video.file_unique_id}")
            return
        if db.video_exists(checksum=video_info.checksum):
            logger.info(f"Duplicate video (checksum): {video_info.checksum}")
            await update.message.reply_text("⚠️ Duplicate video detected.")
            return

    if DATABASE_ENABLED:
        db.add_video(
            file_id=video.file_id,
            file_unique_id=video.file_unique_id,
            checksum=video_info.checksum,
            file_size=video_info.file_size,
            duration=video_info.duration,
            width=video_info.width,
            height=video_info.height,
            chat_id=update.effective_chat.id,
            user_id=update.effective_user.id
        )
        db.increment_stat("total_videos")

    batch_count = settings.get(ConfigKeys.BATCH_COUNT, 0) + 1
    settings.set(ConfigKeys.BATCH_COUNT, batch_count)

    if batch_count % 5 == 0:
        await update.message.reply_text(
            f"📥 Received {batch_count} videos…\n"
            f"💾 {format_size(video_info.file_size)} | ⏱️ {format_duration(video_info.duration)}"
        )

    if "album" not in context.user_data:
        context.user_data["album"] = []

    context.user_data["album"].append(video_info)

    album_size = settings.get(ConfigKeys.ALBUM_SIZE, 10)
    if len(context.user_data["album"]) >= album_size:
        await send_album(update, context)


@admin_only
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming photos"""
    if not settings.get(ConfigKeys.ACCEPT_PHOTOS, True):
        await update.message.reply_text("🚫 Photos are currently disabled. Use /toggle_photos to enable.")
        return

    message = update.message
    photo = message.photo[-1]

    if DATABASE_ENABLED and db.photo_exists(photo.file_unique_id):
        await update.message.reply_text("⚠️ Duplicate photo detected.")
        return

    if DATABASE_ENABLED:
        db.add_photo(
            file_id=photo.file_id,
            file_unique_id=photo.file_unique_id,
            chat_id=update.effective_chat.id,
            user_id=update.effective_user.id
        )

    if settings.get(ConfigKeys.PHOTO_APPROVAL_MODE, False):
        context.user_data["pending_photo"] = {
            "file_id": photo.file_id,
            "file_unique_id": photo.file_unique_id
        }

        if DATABASE_ENABLED:
            db.add_pending_approval("photo", photo.file_id, update.effective_chat.id, update.effective_user.id)

        await update.message.reply_text(
            "🖼️ Photo received.\nUse /approve or /reject",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Approve", callback_data="approve_photo"),
                InlineKeyboardButton("❌ Reject", callback_data="reject_photo")
            ]])
        )
    else:
        strip_meta = settings.get(ConfigKeys.STRIP_METADATA, True)

        if strip_meta:
            await update.message.reply_photo(photo.file_id)
        else:
            caption = message.caption if message.caption else None
            await update.message.reply_photo(photo.file_id, caption=caption)

        if DATABASE_ENABLED:
            db.increment_stat("total_photos")


@admin_only
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming documents"""
    if not settings.get(ConfigKeys.ACCEPT_DOCUMENTS, False):
        await update.message.reply_text("🚫 Documents are currently disabled. Use /toggle_documents to enable.")
        return

    message = update.message
    doc = message.document

    if DATABASE_ENABLED and db.document_exists(doc.file_unique_id):
        await update.message.reply_text("⚠️ Duplicate document detected.")
        return

    if DATABASE_ENABLED:
        db.add_document(
            file_id=doc.file_id,
            file_unique_id=doc.file_unique_id,
            file_name=doc.file_name,
            file_size=doc.file_size or 0,
            mime_type=doc.mime_type or "unknown",
            chat_id=update.effective_chat.id,
            user_id=update.effective_user.id
        )

    if settings.get(ConfigKeys.DOCUMENT_APPROVAL_MODE, False):
        context.user_data["pending_document"] = {
            "file_id": doc.file_id,
            "file_unique_id": doc.file_unique_id,
            "file_name": doc.file_name
        }

        if DATABASE_ENABLED:
            db.add_pending_approval("document", doc.file_id, update.effective_chat.id, update.effective_user.id)

        await update.message.reply_text(
            f"📄 Document received: {doc.file_name}\nUse /approve or /reject",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Approve", callback_data="approve_doc"),
                InlineKeyboardButton("❌ Reject", callback_data="reject_doc")
            ]])
        )
    else:
        strip_meta = settings.get(ConfigKeys.STRIP_METADATA, True)

        if strip_meta:
            await update.message.reply_document(document=doc.file_id)
        else:
            caption = message.caption if message.caption else None
            await update.message.reply_document(document=doc.file_id, caption=caption)

        if DATABASE_ENABLED:
            db.increment_stat("total_documents")


# ==================== ALBUM SENDING ====================

async def send_album(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: Optional[int] = None):
    """Send album of videos"""
    album = context.user_data.get("album", [])
    if not album:
        return

    delay = random.uniform(1.5, 3)
    await asyncio.sleep(delay)

    media_group = []
    strip_meta = settings.get(ConfigKeys.STRIP_METADATA, True)
    auto_caption = settings.get(ConfigKeys.AUTO_CAPTION_MODE, False)
    caption_template = settings.get(ConfigKeys.CAPTION_TEMPLATE, "")

    for idx, video_info in enumerate(album):
        caption = None

        if not strip_meta:
            if auto_caption and caption_template:
                caption = caption_template.format(
                    index=idx+1,
                    total=len(album),
                    duration=format_duration(video_info.duration),
                    size=format_size(video_info.file_size)
                )
            elif auto_caption:
                caption = f"📹 Video {idx+1}/{len(album)} | ⏱️ {format_duration(video_info.duration)}"

        media = InputMediaVideo(
            media=video_info.file_id,
            caption=caption
        )
        media_group.append(media)

    max_retries = 3
    target_chat = chat_id or update.effective_chat.id

    for attempt in range(max_retries):
        try:
            if update and update.message:
                await update.message.reply_media_group(media_group)
            else:
                await context.bot.send_media_group(target_chat, media_group)

            context.user_data["album"] = []

            if DATABASE_ENABLED:
                db.increment_stat("total_batches")
                for video in album:
                    db.mark_video_sent(video.file_unique_id)

            if update and update.message:
                await update.message.reply_text(f"✅ Sent batch of {len(album)} videos")
            return

        except RetryAfter as e:
            wait_time = e.retry_after + 1
            logger.warning(f"Rate limited, waiting {wait_time}s")
            await asyncio.sleep(wait_time)
        except Exception as e:
            logger.error(f"Error sending album: {e}")
            await asyncio.sleep(2)


async def send_album_to_chat(app, chat_id, album: List[VideoInfo]):
    """Send album to specific chat"""
    if not album:
        return

    strip_meta = settings.get(ConfigKeys.STRIP_METADATA, True)

    media_group = []
    for video_info in album:
        media = InputMediaVideo(
            media=video_info.file_id,
            caption=None if strip_meta else f"📹 {format_duration(video_info.duration)}"
        )
        media_group.append(media)

    max_retries = 3
    for attempt in range(max_retries):
        try:
            await app.bot.send_media_group(chat_id, media_group)
            if DATABASE_ENABLED:
                db.increment_stat("total_batches")
            return
        except Exception as e:
            logger.error(f"Failed to send album (attempt {attempt + 1}): {e}")
            await asyncio.sleep(2 ** attempt)


# ==================== CALLBACK HANDLER ====================

@admin_only
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button callbacks"""
    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "stats":
        await stats_command(update, context)
    elif data == "settings":
        await show_settings_menu(update, context)
    elif data == "photo_menu":
        await show_photo_menu(update, context)
    elif data == "doc_menu":
        await show_doc_menu(update, context)
    elif data == "admin_menu":
        await show_admin_menu(update, context)
    elif data == "toggle_accept_photos":
        settings.toggle(ConfigKeys.ACCEPT_PHOTOS)
        await show_photo_menu(update, context)
    elif data == "toggle_photo_approval":
        settings.toggle(ConfigKeys.PHOTO_APPROVAL_MODE)
        await show_photo_menu(update, context)
    elif data == "toggle_accept_docs":
        settings.toggle(ConfigKeys.ACCEPT_DOCUMENTS)
        await show_doc_menu(update, context)
    elif data == "toggle_doc_approval":
        settings.toggle(ConfigKeys.DOCUMENT_APPROVAL_MODE)
        await show_doc_menu(update, context)
    elif data == "toggle_strip_meta":
        settings.toggle(ConfigKeys.STRIP_METADATA)
        await show_settings_menu(update, context)
    elif data == "toggle_auto_caption":
        settings.toggle(ConfigKeys.AUTO_CAPTION_MODE)
        await show_settings_menu(update, context)
    elif data == "toggle_compression":
        settings.toggle(ConfigKeys.COMPRESSION_MODE)
        await show_settings_menu(update, context)
    elif data == "adjust_timeout":
        await show_timeout_menu(update, context)
    elif data == "adjust_album":
        await show_album_menu(update, context)
    elif data == "reset_settings":
        settings.reset_to_defaults()
        await query.answer("✅ Settings reset to defaults", show_alert=True)
        await show_settings_menu(update, context)
    elif data == "flush":
        await flush_command(update, context)
    elif data == "help":
        await help_command(update, context)
    elif data == "back_to_main":
        await show_main_menu(update, context)
    elif data == "approve_photo":
        await approve_command(update, context)
    elif data == "reject_photo":
        await reject_command(update, context)
    elif data == "approve_doc":
        await approve_command(update, context)
    elif data == "reject_doc":
        await reject_command(update, context)
    elif data == "list_admins":
        await list_admins_command(update, context)
    elif data.startswith("timeout_"):
        change = int(data.split("_")[1])
        current = settings.get(ConfigKeys.FLUSH_TIMEOUT, 60)
        new_value = max(10, min(3600, current + change))
        settings.set(ConfigKeys.FLUSH_TIMEOUT, new_value)
        await show_timeout_menu(update, context)
    elif data.startswith("album_"):
        change = int(data.split("_")[1])
        current = settings.get(ConfigKeys.ALBUM_SIZE, 10)
        new_value = max(1, min(10, current + change))
        settings.set(ConfigKeys.ALBUM_SIZE, new_value)
        await show_album_menu(update, context)


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show main menu"""
    text = "🏠 <b>Main Menu</b>\n\nSelect an option:"
    await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=get_main_keyboard())


async def show_photo_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show photo settings menu"""
    accept_photos = settings.get(ConfigKeys.ACCEPT_PHOTOS, True)
    photo_approval = settings.get(ConfigKeys.PHOTO_APPROVAL_MODE, False)

    text = (
        f"🖼️ <b>Photo Settings</b>\n\n"
        f"• Accept photos: {'ON ✅' if accept_photos else 'OFF ❌'}\n"
        f"• Approval mode: {'ON ✅' if photo_approval else 'OFF ❌'}\n\n"
        f"<i>When approval mode is ON, photos require manual approval before being sent.</i>"
    )
    await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=get_photo_settings_keyboard())


async def show_doc_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show document settings menu"""
    accept_docs = settings.get(ConfigKeys.ACCEPT_DOCUMENTS, False)
    doc_approval = settings.get(ConfigKeys.DOCUMENT_APPROVAL_MODE, False)

    text = (
        f"📄 <b>Document Settings</b>\n\n"
        f"• Accept documents: {'ON ✅' if accept_docs else 'OFF ❌'}\n"
        f"• Approval mode: {'ON ✅' if doc_approval else 'OFF ❌'}\n\n"
        f"<i>When approval mode is ON, documents require manual approval before being sent.</i>"
    )
    await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=get_document_settings_keyboard())


async def show_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show admin management menu"""
    admins = admin_manager.get_all_admins()

    text = (
        f"👥 <b>Admin Management</b>\n\n"
        f"Total admins: {len(admins)}\n\n"
        f"Use the buttons below or commands:\n"
        f"/addadmin &lt;id&gt; - Add admin\n"
        f"/removeadmin &lt;id&gt; - Remove admin\n"
        f"/listadmins - List all admins"
    )
    await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=get_admin_keyboard())


async def show_timeout_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show timeout adjustment menu"""
    current = settings.get(ConfigKeys.FLUSH_TIMEOUT, 60)

    keyboard = [
        [InlineKeyboardButton("-30s", callback_data="timeout_-30"),
         InlineKeyboardButton("-10s", callback_data="timeout_-10"),
         InlineKeyboardButton("+10s", callback_data="timeout_+10"),
         InlineKeyboardButton("+30s", callback_data="timeout_+30")],
        [InlineKeyboardButton("🔙 Back", callback_data="settings")]
    ]

    await update.callback_query.edit_message_text(
        f"⏱️ <b>Batch Timeout</b>\n\nCurrent: {current} seconds\n\nSelect adjustment:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def show_album_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show album size adjustment menu"""
    current = settings.get(ConfigKeys.ALBUM_SIZE, 10)

    keyboard = [
        [InlineKeyboardButton("-5", callback_data="album_-5"),
         InlineKeyboardButton("-1", callback_data="album_-1"),
         InlineKeyboardButton("+1", callback_data="album_+1"),
         InlineKeyboardButton("+5", callback_data="album_+5")],
        [InlineKeyboardButton("🔙 Back", callback_data="settings")]
    ]

    await update.callback_query.edit_message_text(
        f"📦 <b>Album Size</b>\n\nCurrent: {current} videos\n\nSelect adjustment:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ==================== BATCH WATCHER ====================

async def batch_watcher(app):
    """Watch for batches that need to be flushed"""
    now = time.time()
    last_video_time = settings.get(ConfigKeys.LAST_VIDEO_TIME, 0)
    flush_timeout = settings.get(ConfigKeys.FLUSH_TIMEOUT, 60)

    for chat_id, data in list(app.chat_data.items()):
        album = data.get("album", [])
        if album and now - last_video_time >= flush_timeout:
            try:
                await app.bot.send_message(chat_id, "⏰ Batch timeout reached. Sending remaining videos…")
                await send_album_to_chat(app, chat_id, album)
                data["album"] = []
            except Exception as e:
                logger.error(f"Watcher error for {chat_id}: {e}")


# ==================== ERROR HANDLER ====================

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors"""
    logger.error(f"Update {update} caused error {context.error}")

    if update and update.message:
        try:
            await update.message.reply_text("⚠️ An error occurred. Please try again.")
        except:
            pass


# ==================== POST INIT ====================

async def post_init(app):
    """Initialize bot after startup"""
    settings._load_settings()

    if DATABASE_ENABLED:
        for admin_id in DEFAULT_ADMIN_IDS:
            db.add_admin(admin_id, first_name="Default Admin")

    commands = [
        BotCommand("start", "Show bot status & menu"),
        BotCommand("help", "Show help"),
        BotCommand("id", "Get your Telegram ID"),
        BotCommand("ping", "Check latency"),
        BotCommand("settings", "Open settings menu"),
        BotCommand("stats", "View statistics"),
        BotCommand("addadmin", "Add new admin"),
        BotCommand("removeadmin", "Remove admin"),
        BotCommand("listadmins", "List all admins"),
        BotCommand("toggle_photos", "Toggle accept photos"),
        BotCommand("toggle_documents", "Toggle accept documents"),
        BotCommand("toggle_photo_approval", "Toggle photo approval"),
        BotCommand("toggle_doc_approval", "Toggle document approval"),
        BotCommand("toggle_strip", "Toggle strip metadata"),
        BotCommand("toggle_caption", "Toggle auto-captions"),
        BotCommand("set_caption", "Set caption template"),
        BotCommand("set_timeout", "Set batch timeout"),
        BotCommand("set_album_size", "Set album size"),
        BotCommand("approve", "Approve pending media"),
        BotCommand("reject", "Reject pending media"),
        BotCommand("flush", "Send videos now"),
        BotCommand("clear", "Clear queue"),
        BotCommand("export", "Export stats"),
        BotCommand("dbinfo", "Database info"),
    ]

    await app.bot.set_my_commands(commands)

    app.job_queue.run_repeating(
        lambda ctx: asyncio.create_task(batch_watcher(app)),
        interval=5,
        first=5
    )

    logger.info("✅ Bot initialized with database and enhanced features")


# ==================== MAIN ====================

def main():
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing!")

    DATABASE_URL = os.getenv("DATABASE_URL")
    if DATABASE_URL:
        logger.info("PostgreSQL database URL detected")
    else:
        logger.info("No DATABASE_URL found, using SQLite fallback")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("id", id_command))
    app.add_handler(CommandHandler("ping", ping_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("stats", stats_command))

    app.add_handler(CommandHandler("addadmin", add_admin_command))
    app.add_handler(CommandHandler("removeadmin", remove_admin_command))
    app.add_handler(CommandHandler("listadmins", list_admins_command))

    app.add_handler(CommandHandler("toggle_photos", toggle_photos_command))
    app.add_handler(CommandHandler("toggle_documents", toggle_documents_command))
    app.add_handler(CommandHandler("toggle_photo_approval", toggle_photo_approval_command))
    app.add_handler(CommandHandler("toggle_doc_approval", toggle_doc_approval_command))
    app.add_handler(CommandHandler("toggle_strip", toggle_strip_command))
    app.add_handler(CommandHandler("toggle_caption", toggle_caption_command))
    app.add_handler(CommandHandler("set_caption", set_caption_command))
    app.add_handler(CommandHandler("set_timeout", set_timeout_command))
    app.add_handler(CommandHandler("set_album_size", set_album_size_command))

    app.add_handler(CommandHandler("approve", approve_command))
    app.add_handler(CommandHandler("reject", reject_command))
    app.add_handler(CommandHandler("flush", flush_command))
    app.add_handler(CommandHandler("clear", clear_command))

    app.add_handler(CommandHandler("export", export_command))
    app.add_handler(CommandHandler("dbinfo", db_info_command))

    app.add_handler(CallbackQueryHandler(button_callback))

    app.add_handler(MessageHandler(filters.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    app.add_error_handler(error_handler)

    app.post_init = post_init

    logger.info("🚀 Bot is now polling with database support...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
