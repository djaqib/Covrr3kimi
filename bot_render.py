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

SEND_ALL_ACTIVE = set()
SEND_ALL_CANCELLED = set()

QUEUE_SEND_LOCK = asyncio.Lock()

PORT = int(os.environ.get("PORT", 10000))
WEBHOOK_URL = os.environ.get("RENDER_EXTERNAL_URL", os.environ.get("WEBHOOK_URL", ""))
USE_WEBHOOK = os.environ.get("USE_WEBHOOK", "true").lower() == "true"

DEFAULT_ADMIN_IDS = [8780988453]

for _admin_id in os.getenv("ADMIN_ID", "").split(","):
    try:
        _admin_id = int(_admin_id.strip())
        if _admin_id not in DEFAULT_ADMIN_IDS:
            DEFAULT_ADMIN_IDS.append(_admin_id)
    except (TypeError, ValueError):
        pass

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
    PAUSE_AUTO_FLUSH = "pause_auto_flush"

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
    ConfigKeys.PAUSE_AUTO_FLUSH: False,
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
    """Metadata fingerprint only. It is NOT a content hash and must never decide duplicates."""
    return hashlib.sha256(f"metadata:{file_size}:{duration}:{width}x{height}".encode()).hexdigest()

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
        [InlineKeyboardButton("📊 Stats", callback_data="stats"), InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
        [InlineKeyboardButton("📦 Queue", callback_data="queue_status"), InlineKeyboardButton("🚀 Send now", callback_data="flush")],
        [InlineKeyboardButton("🔎 Search files", callback_data="search_help"), InlineKeyboardButton("⭐ Favorites", callback_data="favorites")],
        [InlineKeyboardButton("🩺 Health", callback_data="health"), InlineKeyboardButton("🖼️ Photos", callback_data="photo_menu")],
        [InlineKeyboardButton("📄 Docs", callback_data="doc_menu"), 
        [InlineKeyboardButton(
            "⚠️ Skipped Videos", callback_data="skipped_menu"
        )],         
InlineKeyboardButton("❓ Help", callback_data="help")]
    ])

def get_queue_keyboard():
    paused = settings.get(ConfigKeys.PAUSE_AUTO_FLUSH, False)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Send now", callback_data="flush"), InlineKeyboardButton("🔄 Refresh", callback_data="queue_status")],
        [InlineKeyboardButton("▶️ Resume auto-send" if paused else "⏸ Pause auto-send", callback_data="toggle_pause")],
        [InlineKeyboardButton("📤 Send All Videos", callback_data="send_all_menu")],
        [InlineKeyboardButton(
            "⚠️ Skipped Videos",
         callback_data="skipped_menu"
        )],            
      [InlineKeyboardButton("🗑 Clear queue", callback_data="clear_queue_confirm"), InlineKeyboardButton("🏠 Menu", callback_data="back_to_main")]
    ])

def get_stats_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧹 Reset statistics", callback_data="reset_stats_confirm")],
        [InlineKeyboardButton("🔙 Back to menu", callback_data="back_to_main")]
    ])

def get_photo_settings_keyboard():
    accept = settings.get(ConfigKeys.ACCEPT_PHOTOS, False)
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
            db_status = "disabled"
            queue_count = None
            status_code = 200
            if DATABASE_ENABLED:
                try:
                    summary = db.get_health_summary()
                    db_status = "connected"
                    queue_count = summary.get("queued", 0)
                except Exception as exc:
                    db_status = "error"
                    status_code = 503
                    logger.error("Health-check database failure: %s", exc)
            self.send_response(status_code)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "healthy" if status_code == 200 else "degraded", "timestamp": datetime.now().isoformat(), "database": db_status, "queued_items": queue_count, "mode": "webhook" if USE_WEBHOOK else "polling"}).encode())
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
/stats - Statistics dashboard
/queue - Queue status
/search &lt;term&gt; - Search saved videos
/favorites - Favorites
/health - Health monitor
/dbinfo - Database info"""
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=get_main_keyboard())
    else:
        await update.effective_message.reply_text(text, parse_mode="HTML")

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
            settings.set(ConfigKeys.ALBUM_SIZE, max(2, min(10, v)))
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
    photos = settings.get(ConfigKeys.ACCEPT_PHOTOS, False)
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
    target = update.effective_message
    if context.user_data.get("pending_photo"):
        photo = context.user_data.pop("pending_photo")
        await context.bot.send_photo(chat_id=update.effective_chat.id, photo=photo["file_id"])
        if DATABASE_ENABLED: db.increment_stat("total_photos")
        if update.callback_query: await update.callback_query.edit_message_text("✅ Photo approved")
        else: await target.reply_text("✅ Approved")
    elif context.user_data.get("pending_document"):
        doc = context.user_data.pop("pending_document")
        await context.bot.send_document(chat_id=update.effective_chat.id, document=doc["file_id"], filename=doc.get("file_name"))
        if DATABASE_ENABLED: db.increment_stat("total_documents")
        if update.callback_query: await update.callback_query.edit_message_text("✅ Document approved")
        else: await target.reply_text("✅ Approved")
    else:
        if update.callback_query: await update.callback_query.edit_message_text("ℹ️ Nothing pending")
        elif target: await target.reply_text("ℹ️ Nothing pending")

@admin_only
async def reject_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = update.effective_message
    if context.user_data.get("pending_photo"):
        context.user_data.pop("pending_photo", None)
        if DATABASE_ENABLED: db.increment_stat("rejected_photos")
        text = "❌ Photo rejected"
    elif context.user_data.get("pending_document"):
        context.user_data.pop("pending_document", None)
        if DATABASE_ENABLED: db.increment_stat("rejected_documents")
        text = "❌ Document rejected"
    else:
        text = "ℹ️ Nothing pending"
    if update.callback_query: await update.callback_query.edit_message_text(text)
    elif target: await target.reply_text(text)

@admin_only
async def flush_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id, user_id = update.effective_chat.id, update.effective_user.id
    before = len(db.get_queue(chat_id, user_id)) if DATABASE_ENABLED else len(context.user_data.get("album", []))
    if not before:
        msg = "ℹ️ Your queue is empty."
        if update.callback_query: await update.callback_query.edit_message_text(msg, reply_markup=get_queue_keyboard())
        else: await update.effective_message.reply_text(msg, reply_markup=get_queue_keyboard())
        return
    sent = 0
    while True:
        remaining = len(db.get_queue(chat_id, user_id)) if DATABASE_ENABLED else len(context.user_data.get("album", []))
        if not remaining: break
        if not await send_album(update, context): break
        sent += min(10, remaining)
    remaining = len(db.get_queue(chat_id, user_id)) if DATABASE_ENABLED else len(context.user_data.get("album", []))
    msg = f"✅ Sent {sent} video(s)." if not remaining else f"⚠️ Sent {sent}; {remaining} remain queued for retry."
    if update.callback_query: await update.callback_query.edit_message_text(msg, reply_markup=get_queue_keyboard())
    elif update.effective_message: await update.effective_message.reply_text(msg, reply_markup=get_queue_keyboard())

@admin_only
async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if DATABASE_ENABLED:
        cleared = db.clear_queue(update.effective_chat.id, update.effective_user.id)
    else:
        cleared = len(context.user_data.get("album", []))
        context.user_data["album"] = []
    await update.effective_message.reply_text(f"🗑️ Cleared {cleared} queued video(s).")

@admin_only
async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if DATABASE_ENABLED:
        items = db.get_queue(update.effective_chat.id, update.effective_user.id)
        total = len(items)
        size = sum(int(x.get("file_size") or 0) for x in items)
    else:
        items = context.user_data.get("album", [])
        total = len(items)
        size = sum(getattr(x, "file_size", 0) for x in items)
    text = f"📦 <b>Queue status</b>\n\n• Videos waiting: <b>{total}</b>\n• Approx. total size: {format_size(size)}\n• Auto-send: {'PAUSED ⏸' if settings.get(ConfigKeys.PAUSE_AUTO_FLUSH, False) else 'ON ▶️'}"
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=get_queue_keyboard())
    else:
        await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=get_queue_keyboard())


@admin_only
async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args).strip()

    if not query:
        await update.effective_message.reply_text(
            "🔎 Send a search term, ID fragment, duration, or file size. Example: /search 120"
        )
        return

    if not DATABASE_ENABLED:
        await update.effective_message.reply_text(
            "Search needs the database enabled."
        )
        return

    results = db.search_saved_files(
        update.effective_user.id, query, limit=10
    )

    if not results:
        await update.effective_message.reply_text(
            "No saved videos matched that search."
        )
        return

    for item in results:
        kind = item["type"]

        detail = (
            f"\n• Duration: {format_duration(item['duration'])}"
            if kind == "video"
            else ""
        )

        text = (
            f"📁 <b>{item.get('label') or kind.title()}</b>{detail}\n"
            f"• Size: {format_size(item['file_size'])}\n"
            f"• Status: {item['status']}\n"
            f"• ID: <code>{item['unique_id']}</code>"
        )

        
        buttons = [
            InlineKeyboardButton(
                "▶️ Open / Send",
                callback_data=f"playfav:{kind}:{item['unique_id']}",
            ),
            InlineKeyboardButton(
                "⭐ Toggle favorite",
                callback_data=f"favorite:{kind}:{item['unique_id']}",
            ),
        ]

        if kind == "video":
            buttons.append(
                InlineKeyboardButton(
                    "🗑 Remove",
                    callback_data=f"delete_confirm:video:{item['unique_id']}",
                )
            )

        kb = InlineKeyboardMarkup([buttons])


        await update.effective_message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=kb
        )

@admin_only
async def favorites_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not DATABASE_ENABLED:
        await update.effective_message.reply_text("Favorites need the database enabled.")
        return
    items = db.get_favorites(update.effective_user.id, limit=20)
    if not items:
        text = "⭐ You have no favorites yet. Use /search to find saved videos, then tap Toggle favorite."
        if update.callback_query: await update.callback_query.edit_message_text(text, reply_markup=get_main_keyboard())
        else: await update.effective_message.reply_text(text, reply_markup=get_main_keyboard())
        return
    for item in items:
        label = item.get("label") or item["media_type"]
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Send again", callback_data=f"playfav:{item['media_type']}:{item['file_unique_id']}")]])
        await context.bot.send_message(update.effective_chat.id, f"⭐ {label}\n<code>{item['file_unique_id']}</code>", parse_mode="HTML", reply_markup=kb)

@admin_only
async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        summary = db.get_health_summary() if DATABASE_ENABLED else {}
        db_status = "Connected" if DATABASE_ENABLED else "Disabled"
        queue = summary.get("queued", 0)
        videos = summary.get("videos", 0)
        favorites = summary.get("favorites", 0)
        text = f"🩺 <b>Health monitor</b>\n\n• Database: {db_status}\n• Saved videos: {videos}\n• Queue items: {queue}\n• Favorites: {favorites}\n• Auto-send: {'Paused' if settings.get(ConfigKeys.PAUSE_AUTO_FLUSH, False) else 'Running'}\n• Uptime: {timedelta(seconds=max(0, int(time.time() - (db.get_stat('bot_start_time') if DATABASE_ENABLED else time.time()))))}"
    except Exception as exc:
        logger.exception("Health command failed")
        text = f"⚠️ Health check failed: {exc.__class__.__name__}"
    if update.callback_query: await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=get_main_keyboard())
    else: await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=get_main_keyboard())

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
    queue_count = len(db.get_queue(update.effective_chat.id, update.effective_user.id)) if DATABASE_ENABLED else len(context.user_data.get("album", []))
    favorite_count = db.get_favorite_count(update.effective_user.id) if DATABASE_ENABLED else 0
    text = f"""📊 <b>Statistics Dashboard</b>

<b>Processed:</b>
• Videos saved: {videos}
• Photos sent/approved: {photos}
• Documents sent/approved: {docs}
• Video batches sent: {batches}
• Videos currently queued: {queue_count}
• Favorites saved: {favorite_count}

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
    message = update.effective_message
    video = message.video if message else None
    if not video:
        return
    checksum = generate_checksum(video.file_size or 0, video.duration or 0, video.width or 0, video.height or 0)

    # Telegram's file_unique_id is the reliable duplicate key. Metadata fingerprints
    # are deliberately not used to reject files because unrelated videos can share metadata.
    if DATABASE_ENABLED and db.video_exists(file_unique_id=video.file_unique_id):
        db.increment_stat("duplicate_videos")
        await message.reply_text("⚠️ Exact duplicate video skipped.")
        return
    if DATABASE_ENABLED:
        inserted = db.add_video(video.file_id, video.file_unique_id, checksum,
                    video.file_size or 0, video.duration or 0, video.width or 0,
                    video.height or 0, update.effective_chat.id, update.effective_user.id)
        if not inserted:
            db.increment_stat("duplicate_videos")
            await message.reply_text("⚠️ This video was already saved or could not be recorded, so I skipped it safely.")
            return
        payload = {"file_id":video.file_id,"file_unique_id":video.file_unique_id,"file_size":video.file_size or 0,"duration":video.duration or 0,"width":video.width or 0,"height":video.height or 0,"checksum":checksum}
        if not db.enqueue_video(update.effective_chat.id, update.effective_user.id, payload):
            try: db.remove_video_record(video.file_unique_id)
            except Exception: logger.exception("Could not roll back video record after queue failure")
            await message.reply_text("⚠️ Could not save this video to the persistent queue. Please resend it.")
            return
        db.increment_stat("total_videos")
        progress_count = db.get_stat("total_videos")
    else:
        context.user_data.setdefault("album", []).append(VideoInfo(video.file_id, video.file_unique_id, video.file_size or 0, video.duration or 0, video.width or 0, video.height or 0, time.time(), checksum))
        progress_count = len(context.user_data.get("album", []))

    settings.set(ConfigKeys.LAST_VIDEO_TIME, time.time())
    if not settings.get(ConfigKeys.PAUSE_AUTO_FLUSH, False):
        count = len(db.get_queue(update.effective_chat.id, update.effective_user.id)) if DATABASE_ENABLED else len(context.user_data.get("album", []))
        if count >= max(2, min(10, int(settings.get(ConfigKeys.ALBUM_SIZE, 10)))):
            await send_album(update, context)
    # Avoid spamming the chat for every clip. Queue status is available by button or /queue.
    if progress_count and progress_count % 50 == 0:
        state = "paused" if settings.get(ConfigKeys.PAUSE_AUTO_FLUSH, False) else "active"
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"📥 Progress: {progress_count} videos saved. Auto-send is {state}.")
    try:
        await message.delete()
    except Exception:
        pass

@admin_only
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not settings.get(ConfigKeys.ACCEPT_PHOTOS, False):
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


async def send_album_to_chat(app, chat_id, user_id, album: List[Dict[str, Any]]):
    async with QUEUE_SEND_LOCK:
        # Re-read the queue after acquiring the lock.
        # Never send a stale batch fetched by another watcher.
        if DATABASE_ENABLED:
            current_queue = db.get_queue(chat_id, user_id)
            items = current_queue[:10]
        else:
            items = album[:10]

        if not items:
            return False

        try:
            if len(items) == 1:
                await app.bot.send_video(
                    chat_id=chat_id,
                    video=items[0]["file_id"],
                )
            else:
                media = [
                    InputMediaVideo(media=v["file_id"])
                    for v in items
                ]
                await app.bot.send_media_group(
                    chat_id=chat_id,
                    media=media,
                )

            if DATABASE_ENABLED:
                db.remove_queue_items(
                    chat_id,
                    user_id,
                    [v["file_unique_id"] for v in items],
                )
                if len(items) > 1:
                    db.increment_stat("total_batches")
                for item in items:
                    db.mark_video_sent(item["file_unique_id"])

            return True

        except Exception as exc:
            logger.error(
                "Auto-send failed; queue retained for retry: %s", exc
            )
            return False



async def send_all_worker(app, chat_id, user_id, source, message_id):
    key = (chat_id, user_id)
    sent = 0
    skipped = 0
    batch_number = 0

    try:
        if source == "library":
            items = db.get_all_saved_videos()
            total = len(items)
            position = 0

            if not items:
                await app.bot.edit_message_text(
                    "📚 Your saved library is empty.",
                    chat_id=chat_id,
                    message_id=message_id,
                )
                return
        else:
            items = []
            total = len(db.get_queue(chat_id, user_id))

            if total == 0:
                await app.bot.edit_message_text(
                    "📦 Your queue is empty.",
                    chat_id=chat_id,
                    message_id=message_id,
                )
                return

        while True:
            if key in SEND_ALL_CANCELLED:
                break

            if source == "library":
                batch = items[position:position + 10]
                if not batch:
                    break
            else:
                batch = db.get_queue(chat_id, user_id)[:10]
                if not batch:
                    break

            successful = []
            failed = []

            try:
                if len(batch) == 1:
                    await app.bot.send_video(
                        chat_id=chat_id,
                        video=batch[0]["file_id"],
                    )
                else:
                    await app.bot.send_media_group(
                        chat_id=chat_id,
                        media=[
                            InputMediaVideo(media=item["file_id"])
                            for item in batch
                        ],
                    )

                successful = list(batch)

            except RetryAfter as exc:
                await asyncio.sleep(float(exc.retry_after) + 1)
                continue

            except TelegramError as exc:
                if "media_file_invalid" not in str(exc).lower():
                    logger.exception(
                        "Bulk send failed at batch %s after %s videos",
                        batch_number + 1,
                        sent,
                    )
                    await app.bot.edit_message_text(
                        f"⚠️ Sending stopped after {sent} videos.\n"
                        f"Error: {str(exc)[:250]}\n"
                        "The current batch has not been deliberately skipped. "
                        "Check the logs before retrying.",
                        chat_id=chat_id,
                        message_id=message_id,
                    )
                    return

                # The album contains at least one invalid media reference.
                # Test each video separately to isolate the bad reference.
                logger.warning(
                    "Invalid media in batch %s. Testing videos individually.",
                    batch_number + 1,
                )

                for item in batch:
                    if key in SEND_ALL_CANCELLED:
                        break

                    try:
                        await app.bot.send_video(
                            chat_id=chat_id,
                            video=item["file_id"],
                        )
                        successful.append(item)

                    except RetryAfter as retry:
                        await asyncio.sleep(float(retry.retry_after) + 1)
                        try:
                            await app.bot.send_video(
                                chat_id=chat_id,
                                video=item["file_id"],
                            )
                            successful.append(item)
                        
                        except TelegramError as item_exc:
                            failed.append(item)
                            db.add_skipped_video(
                       
                                item,
                                str(item_exc),
                                source,
                            )
                            logger.warning(
                                "Skipped video %s: %s",
                                item.get("file_unique_id"),
                                item_exc,
                            )

                            db.add_skipped_video(
                                item,
                                str(item_exc),
                                source,
                            )                   
                        except TelegramError as item_exc:
                            failed.append(item)
                            db.add_skipped_video(
                            item,
                            str(item_exc),
                            source,
                        )
                        logger.warning(
                            "Skipped video %s: %s",
                            item.get("file_unique_id"),
                            item_exc,
                        )


            # Advance past this batch in the shared library, even when
            # individual videos are invalid. Keep library records intact.
            if source == "library":
                position += len(batch)

            # Remove processed items from the queue so an invalid reference
            # cannot block every subsequent batch. Keep failed videos saved
            # in the shared library for later inspection or deletion.
            if source == "queue":
                processed_ids = [
                    item["file_unique_id"] for item in batch
                ]
                db.remove_queue_items(chat_id, user_id, processed_ids)

            for item in successful:
                db.mark_video_sent(item["file_unique_id"])

            if len(successful) > 1:
                db.increment_stat("total_batches")

            sent += len(successful)
            skipped += len(failed)
            batch_number += 1

            progress = (
                position if source == "library"
                else sent + skipped
            )

            await app.bot.edit_message_text(
                f"📤 Sending videos...\n\n"
                f"Source: {'Shared library' if source == 'library' else 'Queue'}\n"
                f"Videos sent: {sent}\n"
                f"Videos skipped: {skipped}\n"
                f"Batches processed: {batch_number}\n"
                f"Progress: {min(progress, total)}/{total}",
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "⏹ Stop",
                        callback_data="send_all_cancel",
                    )
                ]]),
            )

            if source == "library":
                more = position < len(items)
            else:
                more = bool(db.get_queue(chat_id, user_id))

            if more and key not in SEND_ALL_CANCELLED:
                await asyncio.sleep(6)

        if key in SEND_ALL_CANCELLED:
            result = (
                f"⏹ Stopped.\nVideos sent: {sent}\n"
                f"Videos skipped: {skipped}"
            )
        else:
            result = (
                f"✅ Retrieval finished!\n"
                f"Videos sent: {sent}\n"
                f"Videos skipped: {skipped}"
            )

        await app.bot.edit_message_text(
            result,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=get_queue_keyboard(),
        )

    except Exception:
        logger.exception("Bulk video send failed")
        try:
            await app.bot.edit_message_text(
                f"⚠️ Bulk send encountered an error.\n"
                f"Videos sent: {sent}\nVideos skipped: {skipped}\n"
                "Check the hosting logs before retrying.",
                chat_id=chat_id,
                message_id=message_id,
            )
        except Exception:
            pass

    finally:
        SEND_ALL_ACTIVE.discard(key)
        SEND_ALL_CANCELLED.discard(key)


async def show_skipped_menu(query, offset=0):
    if not DATABASE_ENABLED:
        await query.edit_message_text(
            "⚠️ Skipped-video storage requires the database."
        )
        return

    try:
        total = db.get_skipped_video_count()
        items = db.get_skipped_videos(limit=8, offset=offset)

        if not items:
            await query.edit_message_text(
                "✅ No skipped videos!\n\n"
                "Videos that fail during bulk sending will appear here.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "🔙 Main menu",
                        callback_data="back_to_main"
                    )]
                ]),
            )
            return

        buttons = []
        lines = [
            "⚠️ SKIPPED VIDEOS",
            "",
            f"Total saved for review: {total}",
            "",
        ]

        for index, item in enumerate(items, start=offset + 1):
            uid = item["file_unique_id"]
            reason = str(item.get("reason") or "Unknown error")
            attempts = item.get("attempts", 1)

            lines.append(
                f"{index}. ID: {uid}\n"
                f"   Attempts: {attempts}\n"
                f"   Reason: {reason[:100]}\n"
            )

            buttons.append([
                InlineKeyboardButton(
                    f"▶️ Retry #{index}",
                    callback_data=f"skip_retry:{uid}",
                ),
                InlineKeyboardButton(
                    "🧹 Remove from list",
                    callback_data=f"skip_remove:{uid}",
                ),
            ])
            buttons.append([
                InlineKeyboardButton(
                    f"🗑 Delete #{index} permanently",
                    callback_data=f"delete_confirm:video:{uid}",
                )
            ])

        navigation = []
        if offset > 0:
            navigation.append(
                InlineKeyboardButton(
                    "⬅️ Previous",
                    callback_data=f"skipped_page:{max(0, offset - 8)}",
                )
            )
        if offset + 8 < total:
            navigation.append(
                InlineKeyboardButton(
                    "Next ➡️",
                    callback_data=f"skipped_page:{offset + 8}",
                )
            )
        if navigation:
            buttons.append(navigation)

        buttons.append([
            InlineKeyboardButton(
                "🧹 Clear skipped list",
                callback_data="skipped_clear_confirm",
            )
        ])
        buttons.append([
            InlineKeyboardButton(
                "🔙 Main menu",
                callback_data="back_to_main",
            )
        ])

        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    except Exception:
        logger.exception("Could not display skipped videos")
        await query.edit_message_text(
            "⚠️ Could not load skipped videos. Check the hosting logs."
        )
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    # Favorite/play callbacks provide their own toast; acknowledge all other callbacks here.
    if not data.startswith("favorite:") and not data.startswith("playfav:"):
        await query.answer()

    if data == "skipped_menu":
        await show_skipped_menu(query)

    elif data.startswith("skipped_page:"):
        offset = max(0, int(data.split(":", 1)[1]))
        await show_skipped_menu(query, offset)

    elif data.startswith("skip_remove:"):
        unique_id = data.split(":", 1)[1]
        db.remove_skipped_video(unique_id)
        await show_skipped_menu(query)

    elif data == "skipped_clear_confirm":
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✅ Yes, clear list",
                    callback_data="skipped_clear_do",
                ),
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data="skipped_menu",
                ),
            ]
        ])
        await query.edit_message_text(
            "Clear all skipped-video entries?\n\n"
            "⚠️ This only clears the list. Your saved videos "
            "will NOT be deleted.",
            reply_markup=kb,
        )

    elif data == "skipped_clear_do":
        count = db.clear_skipped_videos()
        await query.edit_message_text(
            f"✅ Cleared {count} skipped-video entries.\n"
            "Your saved library is unchanged.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "🔙 Main menu",
                    callback_data="back_to_main",
                )]
            ]),
        )

    elif data.startswith("skip_retry:"):
        unique_id = data.split(":", 1)[1]
        item = db.get_media_by_unique_id("video", unique_id)

        if not item:
            db.remove_skipped_video(unique_id)
            await query.edit_message_text(
                "This video is no longer in the library.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "🔙 Skipped videos",
                        callback_data="skipped_menu",
                    )]
                ]),
            )
        else:
            try:
                await context.bot.send_video(
                    chat_id=update.effective_chat.id,
                    video=item["file_id"],
                )
                db.mark_video_sent(unique_id)
                db.remove_skipped_video(unique_id)
                await query.answer("✅ Video sent successfully!")
                await show_skipped_menu(query)
            except RetryAfter as exc:
                await query.answer(
                    f"Telegram rate limit. Try again in "
                    f"{int(float(exc.retry_after)) + 1} seconds.",
                    show_alert=True,
                )
            except TelegramError as exc:
                db.add_skipped_video(
                    item,
                    str(exc),
                    "retry",
                )
                await query.answer(
                    f"❌ Still unable to send: {str(exc)[:150]}",
                    show_alert=True,
                )
    if data == "send_all_menu":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "📚 All saved library videos",
                callback_data="send_all:library",
            )],
            [InlineKeyboardButton(
                "📦 Queued videos only",
                callback_data="send_all:queue",
            )],
            [InlineKeyboardButton("🔙 Back", callback_data="queue_status")],
        ])
        await query.edit_message_text(
            "📤 <b>Send All Videos</b>\n\n"
            "Choose which videos to send. Each batch will be followed "
            "by a 6-second pause.",
            parse_mode="HTML",
            reply_markup=kb,
        )

    elif data.startswith("send_all:"):
        source = data.split(":", 1)[1]
        key = (update.effective_chat.id, update.effective_user.id)

        if source not in ("library", "queue"):
            await query.answer("Invalid selection", show_alert=True)
            return

        if key in SEND_ALL_ACTIVE:
            await query.answer(
                "A bulk send is already running for this account.",
                show_alert=True,
            )
            return

        SEND_ALL_CANCELLED.discard(key)
        SEND_ALL_ACTIVE.add(key)

        await query.edit_message_text(
            "⏳ Starting bulk send...",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "⏹ Stop",
                    callback_data="send_all_cancel",
                )
            ]]),
        )

        context.application.create_task(
            send_all_worker(
                context.application,
                key[0],
                key[1],
                source,
                query.message.message_id,
            )
        )

    elif data == "send_all_cancel":
        key = (update.effective_chat.id, update.effective_user.id)
        if key in SEND_ALL_ACTIVE:
            SEND_ALL_CANCELLED.add(key)
            await query.answer("Stopping after the current batch.")
        else:
            await query.answer("No bulk send is running.")

    elif data.startswith("delete_confirm:"):
        _, media_type, unique_id = data.split(":", 2)
        if media_type != "video":
            await query.answer("Unsupported media type", show_alert=True)
            return

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "🗑 Yes, delete permanently",
                callback_data=f"delete_do:{media_type}:{unique_id}",
            ),
            InlineKeyboardButton(
                "❌ Cancel",
                callback_data="delete_cancel",
            ),
        ]])

        await query.edit_message_reply_markup(reply_markup=kb)
        await query.answer(
            "This deletes the video for every authorized account.",
            show_alert=True,
        )

    elif data.startswith("delete_do:"):
        _, media_type, unique_id = data.split(":", 2)

        if media_type != "video" or not DATABASE_ENABLED:
            await query.answer("Could not delete this video.", show_alert=True)
            return

        deleted = db.delete_saved_video(unique_id)
        if deleted:
            await query.edit_message_text(
                "🗑 Video deleted from the shared library, favorites,"
                "and queues.",
                db.remove_skipped_video(unique_id)
            )
        else:
            await query.edit_message_text(
                "The video was not found. It may already have been deleted."
            )

    elif data == "delete_cancel":
        await query.edit_message_text(
            "Deletion cancelled. The video remains in your library."
        )

    if data == "stats":
        await stats_command(update, context)
    elif data == "queue_status":
        await queue_command(update, context)
    elif data == "toggle_pause":
        settings.toggle(ConfigKeys.PAUSE_AUTO_FLUSH)
        await queue_command(update, context)
    elif data == "clear_queue_confirm":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Clear it", callback_data="clear_queue_do"), InlineKeyboardButton("Cancel", callback_data="queue_status")]])
        await query.edit_message_text("Clear every video currently waiting in your queue? Saved-file records will remain.", reply_markup=kb)
    elif data == "clear_queue_do":
        count = db.clear_queue(update.effective_chat.id, update.effective_user.id) if DATABASE_ENABLED else len(context.user_data.get("album", []))
        context.user_data["album"] = []
        await query.edit_message_text(f"🗑 Cleared {count} queued video(s).", reply_markup=get_queue_keyboard())
    elif data == "search_help":
        await query.edit_message_text("🔎 Search your saved videos by sending /search followed by an ID fragment, duration, or file size. Example: /search 120", reply_markup=get_main_keyboard())
    elif data == "favorites":
        await favorites_command(update, context)
    elif data == "health":
        await health_command(update, context)
    elif data.startswith("favorite:"):
        parts = data.split(":", 2)
        if len(parts) == 3 and DATABASE_ENABLED:
            media_type, unique_id = parts[1], parts[2]
            record = db.get_media_by_unique_id(media_type, unique_id)
            if record:
                added = db.toggle_favorite(update.effective_user.id, media_type, unique_id, record["file_id"], record.get("label") or f"{media_type.title()} {unique_id}")
                await query.answer("Added to favorites ⭐" if added else "Removed from favorites", show_alert=False)
            else:
                await query.answer("Saved file not found", show_alert=True)
        else:
            await query.answer("Invalid favorite", show_alert=True)
    elif data.startswith("playfav:"):
        parts = data.split(":", 2)
        if len(parts) == 3 and DATABASE_ENABLED:
            media_type, unique_id = parts[1], parts[2]
            record = db.get_media_by_unique_id(media_type, unique_id)
        else:
            record = None
            media_type = ""
        if record:
            if media_type == "video": await context.bot.send_video(chat_id=update.effective_chat.id, video=record["file_id"])
            elif media_type == "photo": await context.bot.send_photo(chat_id=update.effective_chat.id, photo=record["file_id"])
            else: await context.bot.send_document(chat_id=update.effective_chat.id, document=record["file_id"])
            await query.answer("Sent")
        else:
            await query.answer("File no longer exists", show_alert=True)
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
        settings.set(ConfigKeys.ALBUM_SIZE, max(2, min(10, current + change)))
        await show_album_menu(update, context)

async def show_photo_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    accept = settings.get(ConfigKeys.ACCEPT_PHOTOS, False)
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
    if settings.get(ConfigKeys.PAUSE_AUTO_FLUSH, False):
        return
    timeout = int(settings.get(ConfigKeys.FLUSH_TIMEOUT, 100))
    if not DATABASE_ENABLED:
        return
    try:
        owners = db.get_expired_queues(timeout)
        for owner in owners:
            chat_id, user_id = owner["chat_id"], owner["user_id"]
            items = db.get_queue(chat_id, user_id)
            while items:
                chunk = items[:10]
                if not await send_album_to_chat(app, chat_id, user_id, chunk):
                    break
                items = db.get_queue(chat_id, user_id)
    except Exception as exc:
        logger.exception("Persistent batch watcher failed: %s", exc)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Error: {context.error}")


async def post_init(app):
    settings._load_settings()

    if DATABASE_ENABLED:
        for admin_id in DEFAULT_ADMIN_IDS:
            db.add_admin(admin_id, first_name="Default Admin")

    commands = [
        BotCommand("start", "Open main menu"),
        BotCommand("help", "Show all commands"),
        BotCommand("queue", "View queued videos"),
        BotCommand("flush", "Send queued videos"),
        BotCommand("search", "Search saved files"),
        BotCommand("favorites", "View favorites"),
        BotCommand("settings", "Open settings"),
        BotCommand("stats", "View statistics"),
        BotCommand("health", "Check bot health"),
    ]

    await app.bot.set_my_commands(commands)

    app.job_queue.run_repeating(
        lambda ctx: asyncio.create_task(batch_watcher(app)),
        interval=5,
        first=5,
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
    app.add_handler(CommandHandler("queue", queue_command))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CommandHandler("favorites", favorites_command))
    app.add_handler(CommandHandler("health", health_command))
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
