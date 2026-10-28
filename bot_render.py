"""
Enhanced Telegram Bot - Render Deployment Version
Uses Webhooks instead of Polling (required for Render)
"""

import os
import sys
import logging
import asyncio
import random
import time
import json
import hashlib
import threading
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Set, Tuple, Any
from dataclasses import dataclass, asdict
from functools import wraps
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

from telegram import Update, InputMediaVideo, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters, CallbackQueryHandler
)
from telegram.error import TelegramError, RetryAfter, TimedOut

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from database import Database, db
    DATABASE_ENABLED = True
except ImportError:
    DATABASE_ENABLED = False
    print("Warning: database module not found")

from logging.handlers import RotatingFileHandler
import os
from threading import Thread
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        RotatingFileHandler("bot.log", maxBytes=5*1024*1024, backupCount=3),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

PORT = int(os.environ.get("PORT", 10000))
WEBHOOK_URL = os.environ.get("RENDER_EXTERNAL_URL", os.environ.get("WEBHOOK_URL", ""))
USE_WEBHOOK = os.environ.get("USE_WEBHOOK", "true").lower() == "true"

DEFAULT_ADMIN_IDS = [8780988453]

class ConfigKeys:
    PHOTO_APPROVAL_MODE = "photo_approval_mode"
    DOCUMENT_APPROVAL_MODE = "document_approval_mode"
    ACCEPT_PHOTOS = "accept_photos"
    ACCEPT_DOCUMENTS = "accept_documents"
    STRIP_METADATA = "strip_metadata"
    AUTO_CAPTION_MODE = "auto_caption_mode"
    CAPTION_TEMPLATE = "caption_template"
    FLUSH_TIMEOUT = "flush_timeout"
    ALBUM_SIZE = "album_size"
    BATCH_COUNT = "batch_count"
    LAST_VIDEO_TIME = "last_video_time"

DEFAULT_SETTINGS = {
    ConfigKeys.PHOTO_APPROVAL_MODE: False,
    ConfigKeys.DOCUMENT_APPROVAL_MODE: False,
    ConfigKeys.ACCEPT_PHOTOS: False,
    ConfigKeys.ACCEPT_DOCUMENTS: True,
    ConfigKeys.STRIP_METADATA: True,
    ConfigKeys.AUTO_CAPTION_MODE: False,
    ConfigKeys.CAPTION_TEMPLATE: "",
    ConfigKeys.FLUSH_TIMEOUT: 100,
    ConfigKeys.ALBUM_SIZE: 10,
    ConfigKeys.BATCH_COUNT: 0,
    ConfigKeys.LAST_VIDEO_TIME: 0,
}

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

class SettingsManager:
    def __init__(self):
        self._cache: Dict[str, Any] = {}
        self._load_settings()

    def _load_settings(self):
        if DATABASE_ENABLED:
            try:
                stored = db.get_all_settings()
                self._cache = {**DEFAULT_SETTINGS, **stored}
            except Exception as e:
                logger.error(f"Error loading settings: {e}")
                self._cache = DEFAULT_SETTINGS.copy()
        else:
            try:
                if os.path.exists("bot_settings.json"):
                    with open("bot_settings.json", 'r') as f:
                        stored = json.load(f)
                    self._cache = {**DEFAULT_SETTINGS, **stored}
                else:
                    self._cache = DEFAULT_SETTINGS.copy()
            except:
                self._cache = DEFAULT_SETTINGS.copy()

    def get(self, key: str, default: Any = None) -> Any:
        return self._cache.get(key, default)

    def set(self, key: str, value: Any):
        self._cache[key] = value
        if DATABASE_ENABLED:
            try:
                db.set_setting(key, value)
            except Exception as e:
                logger.error(f"Error saving: {e}")
        try:
            with open("bot_settings.json", 'w') as f:
                json.dump(self._cache, f, indent=2)
        except:
            pass

    def toggle(self, key: str) -> bool:
        current = self.get(key, False)
        new_value = not current
        self.set(key, new_value)
        return new_value

settings = SettingsManager()

class AdminManager:
    def __init__(self):
        self._default_admins = DEFAULT_ADMIN_IDS.copy()

    def is_admin(self, user_id: int) -> bool:
        if DATABASE_ENABLED:
            try:
                if db.is_admin(user_id):
                    return True
            except:
                pass
        return user_id in self._default_admins

    def add_admin(self, user_id: int, username: str = None, first_name: str = None, last_name: str = None) -> bool:
        if DATABASE_ENABLED:
            try:
                return db.add_admin(user_id, username, first_name, last_name)
            except:
                return False
        if user_id not in self._default_admins:
            self._default_admins.append(user_id)
            return True
        return False

    def remove_admin(self, user_id: int) -> bool:
        if user_id in self._default_admins:
            self._default_admins.remove(user_id)
        if DATABASE_ENABLED:
            try:
                return db.remove_admin(user_id)
            except:
                return False
        return True

    def get_all_admins(self) -> List[Dict]:
        admins = []
        if DATABASE_ENABLED:
            try:
                admins.extend(db.get_all_admins())
            except:
                pass
        existing_ids = {a.get('user_id') for a in admins}
        for uid in self._default_admins:
            if uid not in existing_ids:
                admins.append({'user_id': uid, 'username': None, 'first_name': 'Default Admin', 'source': 'default'})
        return admins

admin_manager = AdminManager()

def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not admin_manager.is_admin(user_id):
            if update.message:
                await update.message.reply_text("🔒 Access denied.")
            elif update.callback_query:
                await update.callback_query.answer("🔒 Access denied", show_alert=True)
            return
        if DATABASE_ENABLED:
            try:
                user = update.effective_user
                db.update_user_activity(user.id, user.username, user.first_name, user.last_name)
            except:
                pass
        return await func(update, context)
    return wrapper

def generate_checksum(file_size: int, duration: int, width: int, height: int) -> str:
    return hashlib.md5(f"{file_size}_{duration}_{width}x{height}".encode()).hexdigest()

def format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024**2:
        return f"{size_bytes/1024:.1f} KB"
    elif size_bytes < 1024**3:
        return f"{size_bytes/1024**2:.1f} MB"
    else:
        return f"{size_bytes/1024**3:.2f} GB"

def format_duration(seconds: int) -> str:
    return f"{seconds // 60:02d}:{seconds % 60:02d}"

def get_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats", callback_data="stats"),
         InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
        [InlineKeyboardButton("🖼️ Photos", callback_data="photo_menu"),
         InlineKeyboardButton("📄 Docs", callback_data="doc_menu")],
        [InlineKeyboardButton("🚀 Flush", callback_data="flush"),
         InlineKeyboardButton("❓ Help", callback_data="help")]
    ])

def get_stats_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧹 Reset statistics", callback_data="reset_stats_confirm")],
        [InlineKeyboardButton("🔙 Back to menu", callback_data="back_to_main")]
    ])

def get_photo_settings_keyboard():
    accept = settings.get(ConfigKeys.ACCEPT_PHOTOS, True)
    approval = settings.get(ConfigKeys.PHOTO_APPROVAL_MODE, False)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{'✅' if accept else '❌'} Accept Photos", callback_data="toggle_accept_photos")],
        [InlineKeyboardButton(f"{'✅' if approval else '❌'} Approval Mode", callback_data="toggle_photo_approval")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]
    ])

def get_document_settings_keyboard():
    accept = settings.get(ConfigKeys.ACCEPT_DOCUMENTS, False)
    approval = settings.get(ConfigKeys.DOCUMENT_APPROVAL_MODE, False)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{'✅' if accept else '❌'} Accept Documents", callback_data="toggle_accept_docs")],
        [InlineKeyboardButton(f"{'✅' if approval else '❌'} Approval Mode", callback_data="toggle_doc_approval")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]
    ])

def get_settings_keyboard():
    strip = settings.get(ConfigKeys.STRIP_METADATA, True)
    caption = settings.get(ConfigKeys.AUTO_CAPTION_MODE, False)
    timeout = settings.get(ConfigKeys.FLUSH_TIMEOUT, 60)
    album = settings.get(ConfigKeys.ALBUM_SIZE, 10)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{'✅' if strip else '❌'} Strip Metadata", callback_data="toggle_strip_meta")],
        [InlineKeyboardButton(f"{'✅' if caption else '❌'} Auto Captions", callback_data="toggle_auto_caption")],
        [InlineKeyboardButton(f"⏱️ {timeout}s", callback_data="adjust_timeout"),
         InlineKeyboardButton(f"📦 {album}", callback_data="adjust_album")],
        [InlineKeyboardButton("🔄 Reset", callback_data="reset_settings"),
         InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]
    ])

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "healthy",
                "timestamp": datetime.now().isoformat(),
                "database": "connected" if DATABASE_ENABLED else "disabled",
                "mode": "webhook" if USE_WEBHOOK else "polling"
            }).encode())
        elif self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            html = '<html><head><title>Bot</title></head><body style="font-family:Arial;text-align:center;padding:50px;">'
            html += '<h1>🤖 Telegram Bot</h1><p>Status: <span style="color:green;">Online</span></p>'
            html += '<p>Check <a href="/health">/health</a></p></body></html>'
            self.wfile.write(html.encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

def run_health_server():
    server = ThreadingHTTPServer(('0.0.0.0', PORT), HealthCheckHandler)
    logger.info(f"Health server on port {PORT}")
    server.serve_forever()

@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total_videos = db.get_stat("total_videos") if DATABASE_ENABLED else 0
    total_photos = db.get_stat("total_photos") if DATABASE_ENABLED else 0
    total_batches = db.get_stat("total_batches") if DATABASE_ENABLED else 0

    text = f"🤖 <b>Bot Ready!</b>\n\n"
    text += f"👤 {update.effective_user.first_name}\n"
    text += f"🆔 <code>{update.effective_user.id}</code>\n\n"
    text += f"📊 Videos: {total_videos} | Photos: {total_photos} | Batches: {total_batches}"

    await update.message.reply_text(text, parse_mode="HTML", reply_markup=get_main_keyboard())

@admin_only
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """📋 <b>Commands:</b>

<b>Admin:</b>
/addadmin &lt;id&gt; - Add admin
/removeadmin &lt;id&gt; - Remove admin
/listadmins - List admins

<b>Media:</b>
/toggle_photos - Toggle photos
/toggle_documents - Toggle docs
/toggle_photo_approval - Photo approval
/toggle_doc_approval - Doc approval
/toggle_strip - Strip metadata

<b>Settings:</b>
/settings - Settings menu
/set_timeout &lt;sec&gt; - Batch timeout
/set_album_size &lt;n&gt; - Album size

<b>Control:</b>
/approve - Approve media
/reject - Reject media
/flush - Send videos now
/clear - Clear queue

<b>Info:</b>
/stats - Statistics
/dbinfo - Database info"""
    await update.message.reply_text(text, parse_mode="HTML")

@admin_only
async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🆔 <code>{update.effective_user.id}</code>", parse_mode="HTML")

@admin_only
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start = time.time()
    msg = await update.message.reply_text("🏓")
    latency = (time.time() - start) * 1000
    await msg.edit_text(f"🏓 {latency:.2f}ms")

@admin_only
async def add_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /addadmin <user_id>")
        return
    try:
        new_id = int(context.args[0])
        username = context.args[1] if len(context.args) > 1 else None
        if admin_manager.is_admin(new_id):
            await update.message.reply_text("⚠️ Already admin")
            return
        if admin_manager.add_admin(new_id, username):
            await update.message.reply_text(f"✅ Added: <code>{new_id}</code>", parse_mode="HTML")
        else:
            await update.message.reply_text("❌ Failed")
    except ValueError:
        await update.message.reply_text("❌ Invalid ID")

@admin_only
async def remove_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /removeadmin <user_id>")
        return
    try:
        admin_id = int(context.args[0])
        if admin_id == update.effective_user.id:
            await update.message.reply_text("❌ Cannot remove yourself")
            return
        if admin_id in DEFAULT_ADMIN_IDS:
            await update.message.reply_text("❌ Cannot remove default")
            return
        if admin_manager.remove_admin(admin_id):
            await update.message.reply_text(f"✅ Removed: <code>{admin_id}</code>", parse_mode="HTML")
        else:
            await update.message.reply_text("❌ Failed")
    except ValueError:
        await update.message.reply_text("❌ Invalid ID")

@admin_only
async def list_admins_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admins = admin_manager.get_all_admins()
    if not admins:
        await update.message.reply_text("No admins")
        return
    text = "👥 <b>Admins:</b>\n\n"
    for i, a in enumerate(admins, 1):
        uid = a.get('user_id', '?')
        name = a.get('first_name', '?')
        uname = a.get('username')
        text += f"{i}. <code>{uid}</code> - {name}"
        if uname:
            text += f" (@{uname})"
        text += "\n"
    await update.message.reply_text(text, parse_mode="HTML")

@admin_only
async def toggle_photos_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = settings.toggle(ConfigKeys.ACCEPT_PHOTOS)
    await update.message.reply_text(f"🖼️ Photos: {'ON ✅' if v else 'OFF ❌'}")

@admin_only
async def toggle_documents_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = settings.toggle(ConfigKeys.ACCEPT_DOCUMENTS)
    await update.message.reply_text(f"📄 Docs: {'ON ✅' if v else 'OFF ❌'}")

@admin_only
async def toggle_photo_approval_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = settings.toggle(ConfigKeys.PHOTO_APPROVAL_MODE)
    await update.message.reply_text(f"🖼️ Approval: {'ON ✅' if v else 'OFF ❌'}")

@admin_only
async def toggle_doc_approval_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = settings.toggle(ConfigKeys.DOCUMENT_APPROVAL_MODE)
    await update.message.reply_text(f"📄 Approval: {'ON ✅' if v else 'OFF ❌'}")

@admin_only
async def toggle_strip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = settings.toggle(ConfigKeys.STRIP_METADATA)
    await update.message.reply_text(f"📝 Strip: {'ON ✅' if v else 'OFF ❌'}")

@admin_only
async def toggle_caption_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = settings.toggle(ConfigKeys.AUTO_CAPTION_MODE)
    await update.message.reply_text(f"📝 Captions: {'ON ✅' if v else 'OFF ❌'}")

@admin_only
async def set_caption_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /set_caption <text>")
        return
    caption = " ".join(context.args)
    settings.set(ConfigKeys.CAPTION_TEMPLATE, caption)
    await update.message.reply_text(f"✅ Caption set")

@admin_only
async def set_timeout_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(f"Current: {settings.get(ConfigKeys.FLUSH_TIMEOUT, 60)}s")
        return
    try:
        v = int(context.args[0])
        if 10 <= v <= 3600:
            settings.set(ConfigKeys.FLUSH_TIMEOUT, v)
            await update.message.reply_text(f"✅ Timeout: {v}s")
        else:
            await update.message.reply_text("❌ 10-3600 only")
    except ValueError:
        await update.message.reply_text("❌ Invalid")

@admin_only
async def set_album_size_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(f"Current: {settings.get(ConfigKeys.ALBUM_SIZE, 10)}")
        return
    try:
        v = int(context.args[0])
        if 1 <= v <= 10:
            settings.set(ConfigKeys.ALBUM_SIZE, v)
            await update.message.reply_text(f"✅ Size: {v}")
        else:
            await update.message.reply_text("❌ 1-10 only")
    except ValueError:
        await update.message.reply_text("❌ Invalid")

@admin_only
async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_settings_menu(update, context)

async def show_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    strip = settings.get(ConfigKeys.STRIP_METADATA, True)
    photos = settings.get(ConfigKeys.ACCEPT_PHOTOS, True)
    docs = settings.get(ConfigKeys.ACCEPT_DOCUMENTS, False)
    p_approval = settings.get(ConfigKeys.PHOTO_APPROVAL_MODE, False)
    d_approval = settings.get(ConfigKeys.DOCUMENT_APPROVAL_MODE, False)
    caption = settings.get(ConfigKeys.AUTO_CAPTION_MODE, False)
    timeout = settings.get(ConfigKeys.FLUSH_TIMEOUT, 60)
    album = settings.get(ConfigKeys.ALBUM_SIZE, 10)

    text = f"""⚙️ <b>Settings</b>

<b>Media:</b>
• Strip metadata: {'ON ✅' if strip else 'OFF ❌'}
• Accept photos: {'ON ✅' if photos else 'OFF ❌'}
• Accept docs: {'ON ✅' if docs else 'OFF ❌'}

<b>Approval:</b>
• Photo approval: {'ON ✅' if p_approval else 'OFF ❌'}
• Doc approval: {'ON ✅' if d_approval else 'OFF ❌'}

<b>Other:</b>
• Auto captions: {'ON ✅' if caption else 'OFF ❌'}
• Timeout: {timeout}s
• Album size: {album}

<i>Settings persist across restarts</i>"""

    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=get_settings_keyboard())
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=get_settings_keyboard())

@admin_only
async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("pending_photo"):
        await update.message.reply_photo(context.user_data["pending_photo"]["file_id"])
        context.user_data["pending_photo"] = None
        if DATABASE_ENABLED:
            db.increment_stat("total_photos")
        await update.message.reply_text("✅ Approved")
    elif context.user_data.get("pending_document"):
        doc = context.user_data["pending_document"]
        await update.message.reply_document(document=doc["file_id"], filename=doc.get("file_name"))
        context.user_data["pending_document"] = None
        if DATABASE_ENABLED:
            db.increment_stat("total_documents")
        await update.message.reply_text("✅ Approved")
    else:
        await update.message.reply_text("ℹ️ Nothing pending")

@admin_only
async def reject_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("pending_photo"):
        context.user_data["pending_photo"] = None
        if DATABASE_ENABLED:
            db.increment_stat("rejected_photos")
        await update.message.reply_text("❌ Rejected")
    elif context.user_data.get("pending_document"):
        context.user_data["pending_document"] = None
        if DATABASE_ENABLED:
            db.increment_stat("rejected_documents")
        await update.message.reply_text("❌ Rejected")
    else:
        await update.message.reply_text("ℹ️ Nothing pending")

@admin_only
async def flush_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    album = context.user_data.get("album", [])
    if not album:
        await update.message.reply_text("ℹ️ No videos")
        return
    await send_album(update, context)
    await update.message.reply_text("✅ Flushed")

@admin_only
async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    await update.message.reply_text(f"🗑️ Cleared {cleared}")

@admin_only
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if DATABASE_ENABLED:
        s = db.get_all_stats()
        videos = s.get("total_videos", 0)
        photos = s.get("total_photos", 0)
        docs = s.get("total_documents", 0)
        batches = s.get("total_batches", 0)
        duplicate_videos = s.get("duplicate_videos", 0)
        duplicate_photos = s.get("duplicate_photos", 0)
        duplicate_docs = s.get("duplicate_documents", 0)
        skipped_photos = s.get("skipped_photos_disabled", 0)
        skipped_docs = s.get("skipped_documents_disabled", 0)
        rejected_photos = s.get("rejected_photos", 0)
        rejected_docs = s.get("rejected_documents", 0)
        start = s.get("bot_start_time", time.time())
    else:
        videos = photos = docs = batches = 0
        duplicate_videos = duplicate_photos = duplicate_docs = 0
        skipped_photos = skipped_docs = rejected_photos = rejected_docs = 0
        start = time.time()

    uptime = timedelta(seconds=max(0, int(time.time() - start)))
    db_label = ('PostgreSQL ✅' if DATABASE_ENABLED and db.use_postgres
                else 'SQLite' if DATABASE_ENABLED else 'Disabled')
    text = f"""📊 <b>Statistics</b>

<b>Processed:</b>
• Videos queued: {videos}
• Photos sent/approved: {photos}
• Documents sent/approved: {docs}
• Video batches sent: {batches}

<b>Skipped:</b>
• Duplicate videos: {duplicate_videos}
• Duplicate photos: {duplicate_photos}
• Duplicate documents: {duplicate_docs}
• Photos while photos were OFF: {skipped_photos}
• Documents while documents were OFF: {skipped_docs}

<b>Rejected:</b>
• Photos: {rejected_photos}
• Documents: {rejected_docs}

• Uptime: {uptime}
• DB: {db_label}"""

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode="HTML", reply_markup=get_stats_keyboard()
        )
    elif update.message:
        await update.message.reply_text(
            text, parse_mode="HTML", reply_markup=get_stats_keyboard()
        )

@admin_only
async def reset_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, reset counters", callback_data="reset_stats_do"),
         InlineKeyboardButton("❌ Cancel", callback_data="stats")]
    ])
    text = ("⚠️ <b>Reset statistics?</b>\n\n"
            "This resets counters only. Your video/photo/document records and duplicate detection database will NOT be deleted.")
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)

@admin_only
async def db_info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not DATABASE_ENABLED:
        await update.message.reply_text("❌ DB disabled")
        return
    db_type = "PostgreSQL" if db.use_postgres else "SQLite"
    with db.get_connection() as conn:
        cursor = conn.cursor()
        tables = ["users", "videos", "photos", "documents", "settings"]
        counts = {}
        for t in tables:
            try:
                cursor.execute(f"SELECT COUNT(*) FROM {t}")
                counts[t] = cursor.fetchone()[0]
            except:
                counts[t] = "?"
        cursor.close()

    text = f"🗄️ <b>Database</b>\n\nType: {db_type}\n\n"
    for t, c in counts.items():
        text += f"• {t}: {c}\n"
    await update.message.reply_text(text, parse_mode="HTML")

@admin_only
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if DATABASE_ENABLED:
        db.set_setting(ConfigKeys.LAST_VIDEO_TIME, time.time())
    try:
        await message.delete()
    except:
        pass
    if not message.video:
        return

    video = message.video
    checksum = generate_checksum(video.file_size or 0, video.duration or 0, video.width or 0, video.height or 0)

    if DATABASE_ENABLED:
        if db.video_exists(file_unique_id=video.file_unique_id):
            db.increment_stat("duplicate_videos")
            return
        if db.video_exists(checksum=checksum):
            db.increment_stat("duplicate_videos")
            await update.effective_message.reply_text("⚠️ Duplicate video skipped")
            return
        inserted = db.add_video(video.file_id, video.file_unique_id, checksum,
                    video.file_size or 0, video.duration or 0, video.width or 0,
                    video.height or 0, update.effective_chat.id, update.effective_user.id)
        if not inserted:
            logger.warning("Video record was not inserted; skipping it without changing processed/duplicate counters")
            return
        db.increment_stat("total_videos")

    batch_count = settings.get(ConfigKeys.BATCH_COUNT, 0) + 1
    settings.set(ConfigKeys.BATCH_COUNT, batch_count)

    if batch_count % 5 == 0:
        await update.message.reply_text(f"📥 {batch_count} videos…")

    if "album" not in context.user_data:
        context.user_data["album"] = []

    context.user_data["album"].append(VideoInfo(
        file_id=video.file_id,
        file_unique_id=video.file_unique_id,
        file_size=video.file_size or 0,
        duration=video.duration or 0,
        width=video.width or 0,
        height=video.height or 0,
        timestamp=time.time(),
        checksum=checksum
    ))

    if len(context.user_data["album"]) >= settings.get(ConfigKeys.ALBUM_SIZE, 10):
        await send_album(update, context)

@admin_only
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not settings.get(ConfigKeys.ACCEPT_PHOTOS, True):
        if DATABASE_ENABLED:
            db.increment_stat("skipped_photos_disabled")
        await update.message.reply_text("🚫 Photos disabled, skipped")
        return

    message = update.message
    photo = message.photo[-1]

    if DATABASE_ENABLED and db.photo_exists(photo.file_unique_id):
        db.increment_stat("duplicate_photos")
        await update.message.reply_text("⚠️ Duplicate photo skipped")
        return

    if DATABASE_ENABLED:
        inserted = db.add_photo(photo.file_id, photo.file_unique_id, update.effective_chat.id, update.effective_user.id)
        if not inserted:
            logger.warning("Photo record was not inserted; skipping it without changing processed/duplicate counters")
            await update.message.reply_text("⚠️ Could not record this photo; skipped to avoid inaccurate statistics")
            return

    if settings.get(ConfigKeys.PHOTO_APPROVAL_MODE, False):
        context.user_data["pending_photo"] = {"file_id": photo.file_id, "file_unique_id": photo.file_unique_id}
        if DATABASE_ENABLED:
            db.add_pending_approval("photo", photo.file_id, update.effective_chat.id, update.effective_user.id)
        await update.message.reply_text(
            "🖼️ Photo received",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅", callback_data="approve_photo"),
                InlineKeyboardButton("❌", callback_data="reject_photo")
            ]])
        )
    else:
        strip = settings.get(ConfigKeys.STRIP_METADATA, True)
        if strip:
            await update.message.reply_photo(photo.file_id)
        else:
            await update.message.reply_photo(photo.file_id, caption=message.caption)
        if DATABASE_ENABLED:
            db.increment_stat("total_photos")

@admin_only
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not settings.get(ConfigKeys.ACCEPT_DOCUMENTS, False):
        if DATABASE_ENABLED:
            db.increment_stat("skipped_documents_disabled")
        await update.message.reply_text("🚫 Documents disabled, skipped")
        return

    message = update.message
    doc = message.document

    if DATABASE_ENABLED and db.document_exists(doc.file_unique_id):
        db.increment_stat("duplicate_documents")
        await update.message.reply_text("⚠️ Duplicate document skipped")
        return

    if DATABASE_ENABLED:
        inserted = db.add_document(doc.file_id, doc.file_unique_id, doc.file_name,
                       doc.file_size or 0, doc.mime_type or "unknown",
                       update.effective_chat.id, update.effective_user.id)
        if not inserted:
            logger.warning("Document record was not inserted; skipping it without changing processed/duplicate counters")
            await update.message.reply_text("⚠️ Could not record this document; skipped to avoid inaccurate statistics")
            return

    if settings.get(ConfigKeys.DOCUMENT_APPROVAL_MODE, False):
        context.user_data["pending_document"] = {
            "file_id": doc.file_id,
            "file_unique_id": doc.file_unique_id,
            "file_name": doc.file_name
        }
        if DATABASE_ENABLED:
            db.add_pending_approval("document", doc.file_id, update.effective_chat.id, update.effective_user.id)
        await update.message.reply_text(
            f"📄 {doc.file_name}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅", callback_data="approve_doc"),
                InlineKeyboardButton("❌", callback_data="reject_doc")
            ]])
        )
    else:
        strip = settings.get(ConfigKeys.STRIP_METADATA, True)
        if strip:
            await update.message.reply_document(document=doc.file_id)
        else:
            await update.message.reply_document(document=doc.file_id, caption=message.caption)
        if DATABASE_ENABLED:
            db.increment_stat("total_documents")

async def send_album(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: Optional[int] = None):
    album = context.user_data.get("album", [])
    if not album:
        return

    await asyncio.sleep(random.uniform(1.5, 3))

    media_group = []
    strip = settings.get(ConfigKeys.STRIP_METADATA, True)
    auto_caption = settings.get(ConfigKeys.AUTO_CAPTION_MODE, False)
    template = settings.get(ConfigKeys.CAPTION_TEMPLATE, "")

    for idx, v in enumerate(album):
        caption = None
        if not strip and auto_caption:
            if template:
                caption = template.format(
                    index=idx+1, total=len(album),
                    duration=format_duration(v.duration),
                    size=format_size(v.file_size)
                )
            else:
                caption = f"📹 {idx+1}/{len(album)}"
        media_group.append(InputMediaVideo(media=v.file_id, caption=caption))

    target = chat_id or update.effective_chat.id

    for attempt in range(3):
        try:
            if update and update.message:
                await update.message.reply_media_group(media_group)
            else:
                await context.bot.send_media_group(target, media_group)
            context.user_data["album"] = []
            if DATABASE_ENABLED:
                db.increment_stat("total_batches")
                for v in album:
                    db.mark_video_sent(v.file_unique_id)
            if update and update.message:
                await update.message.reply_text(f"✅ Sent {len(album)}")
            return
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except Exception as e:
            logger.error(f"Send error: {e}")
            await asyncio.sleep(2)

async def send_album_to_chat(app, chat_id, album: List[VideoInfo]):
    if not album:
        return
    strip = settings.get(ConfigKeys.STRIP_METADATA, True)
    media = [InputMediaVideo(media=v.file_id, caption=None if strip else "📹") for v in album]
    for attempt in range(3):
        try:
            await app.bot.send_media_group(chat_id, media)
            if DATABASE_ENABLED:
                db.increment_stat("total_batches")
            return
        except Exception as e:
            logger.error(f"Error: {e}")
            await asyncio.sleep(2 ** attempt)

@admin_only
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "stats":
        await stats_command(update, context)
    elif data == "reset_stats_confirm":
        await reset_stats_command(update, context)
    elif data == "reset_stats_do":
        if not DATABASE_ENABLED:
            await query.edit_message_text("❌ Database is disabled; statistics cannot be reset.", reply_markup=get_main_keyboard())
        elif db.reset_stats():
            await query.edit_message_text("✅ Statistics counters reset. Duplicate-detection records were kept.", reply_markup=get_stats_keyboard())
        else:
            await query.edit_message_text("❌ Could not reset statistics. Check the bot logs and database connection.", reply_markup=get_stats_keyboard())
    elif data == "settings":
        await show_settings_menu(update, context)
    elif data == "photo_menu":
        await show_photo_menu(update, context)
    elif data == "doc_menu":
        await show_doc_menu(update, context)
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
    elif data == "adjust_timeout":
        await show_timeout_menu(update, context)
    elif data == "adjust_album":
        await show_album_menu(update, context)
    elif data == "reset_settings":
        for k, v in DEFAULT_SETTINGS.items():
            settings.set(k, v)
        await query.answer("✅ Reset", show_alert=True)
        await show_settings_menu(update, context)
    elif data == "flush":
        await flush_command(update, context)
    elif data == "help":
        await help_command(update, context)
    elif data == "back_to_main":
        await query.edit_message_text("🏠 Menu", reply_markup=get_main_keyboard())
    elif data == "approve_photo":
        await approve_command(update, context)
    elif data == "reject_photo":
        await reject_command(update, context)
    elif data == "approve_doc":
        await approve_command(update, context)
    elif data == "reject_doc":
        await reject_command(update, context)
    elif data.startswith("timeout_"):
        change = int(data.split("_")[1])
        current = settings.get(ConfigKeys.FLUSH_TIMEOUT, 60)
        settings.set(ConfigKeys.FLUSH_TIMEOUT, max(10, min(3600, current + change)))
        await show_timeout_menu(update, context)
    elif data.startswith("album_"):
        change = int(data.split("_")[1])
        current = settings.get(ConfigKeys.ALBUM_SIZE, 10)
        settings.set(ConfigKeys.ALBUM_SIZE, max(1, min(10, current + change)))
        await show_album_menu(update, context)

async def show_photo_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    accept = settings.get(ConfigKeys.ACCEPT_PHOTOS, True)
    approval = settings.get(ConfigKeys.PHOTO_APPROVAL_MODE, False)
    text = f"🖼️ <b>Photo Settings</b>\n\n• Accept: {'ON ✅' if accept else 'OFF ❌'}\n• Approval: {'ON ✅' if approval else 'OFF ❌'}"
    await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=get_photo_settings_keyboard())

async def show_doc_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    accept = settings.get(ConfigKeys.ACCEPT_DOCUMENTS, False)
    approval = settings.get(ConfigKeys.DOCUMENT_APPROVAL_MODE, False)
    text = f"📄 <b>Doc Settings</b>\n\n• Accept: {'ON ✅' if accept else 'OFF ❌'}\n• Approval: {'ON ✅' if approval else 'OFF ❌'}"
    await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=get_document_settings_keyboard())

async def show_timeout_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current = settings.get(ConfigKeys.FLUSH_TIMEOUT, 60)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("-30", callback_data="timeout_-30"),
         InlineKeyboardButton("-10", callback_data="timeout_-10"),
         InlineKeyboardButton("+10", callback_data="timeout_+10"),
         InlineKeyboardButton("+30", callback_data="timeout_+30")],
        [InlineKeyboardButton("🔙 Back", callback_data="settings")]
    ])
    await update.callback_query.edit_message_text(f"⏱️ Timeout: {current}s", reply_markup=kb)

async def show_album_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current = settings.get(ConfigKeys.ALBUM_SIZE, 10)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("-5", callback_data="album_-5"),
         InlineKeyboardButton("-1", callback_data="album_-1"),
         InlineKeyboardButton("+1", callback_data="album_+1"),
         InlineKeyboardButton("+5", callback_data="album_+5")],
        [InlineKeyboardButton("🔙 Back", callback_data="settings")]
    ])
    await update.callback_query.edit_message_text(f"📦 Size: {current}", reply_markup=kb)

async def batch_watcher(app):
    now = time.time()
    last = settings.get(ConfigKeys.LAST_VIDEO_TIME, 0)
    timeout = settings.get(ConfigKeys.FLUSH_TIMEOUT, 60)
    for chat_id, data in list(app.chat_data.items()):
        album = data.get("album", [])
        if album and now - last >= timeout:
            try:
                await app.bot.send_message(chat_id, "⏰ Timeout")
                await send_album_to_chat(app, chat_id, album)
                data["album"] = []
            except Exception as e:
                logger.error(f"Watcher: {e}")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Error: {context.error}")

async def post_init(app):
    settings._load_settings()
    if DATABASE_ENABLED:
        for admin_id in DEFAULT_ADMIN_IDS:
            db.add_admin(admin_id, first_name="Default Admin")

    commands = [
        BotCommand("start", "Show menu"),
        BotCommand("help", "Help"),
        BotCommand("id", "Your ID"),
        BotCommand("ping", "Latency"),
        BotCommand("settings", "Settings"),
        BotCommand("stats", "Stats"),
        BotCommand("resetstats", "Reset statistics counters"),
        BotCommand("addadmin", "Add admin"),
        BotCommand("removeadmin", "Remove admin"),
        BotCommand("listadmins", "List admins"),
        BotCommand("toggle_photos", "Toggle photos"),
        BotCommand("toggle_documents", "Toggle docs"),
        BotCommand("toggle_photo_approval", "Photo approval"),
        BotCommand("toggle_doc_approval", "Doc approval"),
        BotCommand("toggle_strip", "Strip metadata"),
        BotCommand("toggle_caption", "Auto captions"),
        BotCommand("set_caption", "Set caption"),
        BotCommand("set_timeout", "Set timeout"),
        BotCommand("set_album_size", "Set album size"),
        BotCommand("approve", "Approve"),
        BotCommand("reject", "Reject"),
        BotCommand("flush", "Flush"),
        BotCommand("clear", "Clear"),
        BotCommand("dbinfo", "DB info"),
    ]
    await app.bot.set_my_commands(commands)
    app.job_queue.run_repeating(
        lambda ctx: asyncio.create_task(batch_watcher(app)),
        interval=5, first=5
    )
    logger.info("✅ Bot initialized")

def main():
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN missing!")

    logger.info(f"Mode: {'Webhook' if USE_WEBHOOK else 'Polling'}, Port: {PORT}")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("id", id_command))
    app.add_handler(CommandHandler("ping", ping_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("resetstats", reset_stats_command))
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
    app.add_handler(CommandHandler("dbinfo", db_info_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_error_handler(error_handler)
    app.post_init = post_init

    if USE_WEBHOOK and WEBHOOK_URL:
        webhook_path = f"/webhook/{BOT_TOKEN}"
        full_url = f"{WEBHOOK_URL}{webhook_path}"
        logger.info(f"Webhook: {full_url}")
        
        # app.run_webhook starts the Tornado server on PORT (10000) 
        # and satisfies Render's port requirement automatically!
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=webhook_path,
            webhook_url=full_url,
            drop_pending_updates=True
        )
    else:
        logger.info("Running in polling mode...")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
