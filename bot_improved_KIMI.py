import os
import logging
import asyncio
import random
import time
import json
import hashlib
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Set, Tuple
from dataclasses import dataclass, asdict
from telegram import Update, InputMediaVideo, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters, CallbackQueryHandler, ConversationHandler
)
from telegram.error import TelegramError, RetryAfter, TimedOut

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
# Configuration
# -----------------------------
CONFIG_FILE = "bot_config.json"
STATS_FILE = "bot_stats.json"

# -----------------------------
# Admin IDs (ONLY these users can use bot)
# -----------------------------
ADMIN_IDS = [
    7599601301,  # KR
    8637601933,
    8976017144,
    7467197752,
]

# -----------------------------
# Data Classes for Better State Management
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
    checksum: str  # For duplicate detection

@dataclass
class BatchStats:
    total_videos: int = 0
    total_batches: int = 0
    total_photos: int = 0
    rejected_photos: int = 0
    start_time: float = time.time()
    last_activity: float = time.time()

# -----------------------------
# Global State (Enhanced)
# -----------------------------
class BotState:
    def __init__(self):
        self.video_cache: Set[str] = set()  # file_unique_ids
        self.checksum_cache: Set[str] = set()  # content-based dedup
        self.photo_approval_mode: bool = False
        self.auto_caption_mode: bool = False
        self.compression_mode: bool = False
        self.batch_count: int = 0
        self.last_video_time: float = 0
        self.FLUSH_TIMEOUT: int = 60
        self.ALBUM_SIZE: int = 10
        self.stats = BatchStats()
        self.pending_scheduled: Dict[str, List[VideoInfo]] = {}  # chat_id -> videos
        self.processing_queue: List[Tuple[str, VideoInfo]] = []  # (chat_id, video)

    def to_dict(self):
        return {
            "photo_approval_mode": self.photo_approval_mode,
            "auto_caption_mode": self.auto_caption_mode,
            "compression_mode": self.compression_mode,
            "FLUSH_TIMEOUT": self.FLUSH_TIMEOUT,
            "ALBUM_SIZE": self.ALBUM_SIZE,
            "stats": asdict(self.stats)
        }

    def save_config(self):
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(self.to_dict(), f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save config: {e}")

    def load_config(self):
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r') as f:
                    data = json.load(f)
                    self.photo_approval_mode = data.get("photo_approval_mode", False)
                    self.auto_caption_mode = data.get("auto_caption_mode", False)
                    self.compression_mode = data.get("compression_mode", False)
                    self.FLUSH_TIMEOUT = data.get("FLUSH_TIMEOUT", 60)
                    self.ALBUM_SIZE = data.get("ALBUM_SIZE", 10)
                    stats_data = data.get("stats", {})
                    self.stats = BatchStats(**stats_data)
                    logger.info("Config loaded successfully")
        except Exception as e:
            logger.error(f"Failed to load config: {e}")

# Initialize global state
state = BotState()

# -----------------------------
# Utility Functions
# -----------------------------
def generate_checksum(video_info: VideoInfo) -> str:
    """Generate a checksum for duplicate detection"""
    content = f"{video_info.file_size}_{video_info.duration}_{video_info.width}x{video_info.height}"
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

# -----------------------------
# Admin-only decorator (Enhanced)
# -----------------------------
def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in ADMIN_IDS:
            if update.message:
                await update.message.reply_text("🔒 This bot is private. Access denied.")
            logger.warning(f"Unauthorized access attempt by {user_id}")
            return
        return await func(update, context)
    return wrapper

# -----------------------------
# Keyboard Markups
# -----------------------------
def get_main_keyboard():
    keyboard = [
        [InlineKeyboardButton("📊 Stats", callback_data="stats"),
         InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
        [InlineKeyboardButton("🖼️ Photo Mode", callback_data="toggle_photo"),
         InlineKeyboardButton("📝 Captions", callback_data="toggle_caption")],
        [InlineKeyboardButton("🚀 Flush Now", callback_data="flush"),
         InlineKeyboardButton("❓ Help", callback_data="help")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_settings_keyboard():
    keyboard = [
        [InlineKeyboardButton("⏱️ Timeout +/-", callback_data="adjust_timeout"),
         InlineKeyboardButton("📦 Album Size +/-", callback_data="adjust_album")],
        [InlineKeyboardButton("🗜️ Compression", callback_data="toggle_compression"),
         InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]
    ]
    return InlineKeyboardMarkup(keyboard)

# -----------------------------
# Commands (Admins only)
# -----------------------------
@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        f"🤖 <b>Bot Ready!</b>\n\n"
        f"👤 Your Telegram ID: <code>{update.effective_user.id}</code>\n\n"
        f"📊 Current Stats:\n"
        f"• Videos processed: {state.stats.total_videos}\n"
        f"• Batches sent: {state.stats.total_batches}\n"
        f"• Photos approved: {state.stats.total_photos}\n\n"
        f"Use /help for commands or the menu below:"
    )
    await update.message.reply_text(welcome_text, parse_mode="HTML", reply_markup=get_main_keyboard())


@admin_only
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📋 <b>Available Commands:</b>\n\n"
        "<b>Basic:</b>\n"
        "/start – Show bot status & menu\n"
        "/help – Show this help\n"
        "/id – Get your Telegram ID\n"
        "/ping – Check bot latency\n\n"
        "<b>Settings:</b>\n"
        "/settings – Configure bot\n"
        "/toggle_photo_mode – Toggle photo approval\n"
        "/toggle_caption – Toggle auto-captions\n"
        "/set_timeout &lt;seconds&gt; – Set batch timeout\n"
        "/set_album_size &lt;1-10&gt; – Set album size\n\n"
        "<b>Media:</b>\n"
        "/approve – Approve pending photo\n"
        "/reject – Reject pending photo\n"
        "/flush – Send pending videos now\n"
        "/clear – Clear current queue\n\n"
        "<b>Advanced:</b>\n"
        "/stats – Detailed statistics\n"
        "/schedule &lt;minutes&gt; – Schedule delayed send\n"
        "/export – Export stats to file\n"
        "/backup – Backup configuration"
    )
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


@admin_only
async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uptime = timedelta(seconds=int(time.time() - state.stats.start_time))
    text = (
        f"⚙️ <b>Bot Settings</b>\n\n"
        f"🖼️ Photo approval mode: <b>{'ON ✅' if state.photo_approval_mode else 'OFF ❌'}</b>\n"
        f"📝 Auto captions: <b>{'ON ✅' if state.auto_caption_mode else 'OFF ❌'}</b>\n"
        f"🗜️ Compression: <b>{'ON ✅' if state.compression_mode else 'OFF ❌'}</b>\n"
        f"⏱️ Batch timeout: <b>{state.FLUSH_TIMEOUT}s</b>\n"
        f"📦 Album size: <b>{state.ALBUM_SIZE}</b>\n"
        f"📊 Videos in queue: <b>{sum(len(v) for v in state.pending_scheduled.values())}</b>\n"
        f"⏰ Uptime: <b>{uptime}</b>"
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=get_settings_keyboard())


@admin_only
async def toggle_photo_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state.photo_approval_mode = not state.photo_approval_mode
    state.save_config()
    status = "ON ✅" if state.photo_approval_mode else "OFF ❌"
    await update.message.reply_text(f"🖼️ Photo approval mode is now {status}")


@admin_only
async def toggle_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state.auto_caption_mode = not state.auto_caption_mode
    state.save_config()
    status = "ON ✅" if state.auto_caption_mode else "OFF ❌"

    if state.auto_caption_mode:
        await update.message.reply_text(
            f"📝 Auto-caption mode is now {status}\n\n"
            f"Send /set_caption &lt;your text&gt; to set the caption template\n"
            f"Use {'{filename}'} for original name, {'{duration}'} for length, {'{size}'} for file size"
        )
    else:
        await update.message.reply_text(f"📝 Auto-caption mode is now {status}")


@admin_only
async def set_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /set_caption Your caption text here")
        return

    caption = " ".join(context.args)
    context.user_data["caption_template"] = caption
    await update.message.reply_text(f"✅ Caption template set:\n\n{caption}")


@admin_only
async def set_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(f"Current timeout: {state.FLUSH_TIMEOUT}s\nUsage: /set_timeout 120")
        return

    try:
        new_timeout = int(context.args[0])
        if new_timeout < 10 or new_timeout > 3600:
            await update.message.reply_text("❌ Timeout must be between 10 and 3600 seconds")
            return

        state.FLUSH_TIMEOUT = new_timeout
        state.save_config()
        await update.message.reply_text(f"✅ Batch timeout set to {new_timeout} seconds")
    except ValueError:
        await update.message.reply_text("❌ Invalid number")


@admin_only
async def set_album_size(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(f"Current album size: {state.ALBUM_SIZE}\nUsage: /set_album_size 5")
        return

    try:
        new_size = int(context.args[0])
        if new_size < 1 or new_size > 10:
            await update.message.reply_text("❌ Album size must be between 1 and 10")
            return

        state.ALBUM_SIZE = new_size
        state.save_config()
        await update.message.reply_text(f"✅ Album size set to {new_size}")
    except ValueError:
        await update.message.reply_text("❌ Invalid number")


@admin_only
async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file_id = context.user_data.get("pending_photo")
    if not file_id:
        await update.message.reply_text("ℹ️ No pending photo.")
        return

    await update.message.reply_photo(file_id)
    context.user_data["pending_photo"] = None
    state.stats.total_photos += 1
    state.save_config()


@admin_only
async def reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("pending_photo"):
        context.user_data["pending_photo"] = None
        state.stats.rejected_photos += 1
        await update.message.reply_text("❌ Photo rejected.")
    else:
        await update.message.reply_text("ℹ️ No pending photo.")


@admin_only
async def flush(update: Update, context: ContextTypes.DEFAULT_TYPE):
    album = context.user_data.get("album", [])
    if not album:
        await update.message.reply_text("ℹ️ No pending videos.")
        return

    await send_album(update, context)
    await update.message.reply_text("✅ Flushed remaining videos.")


@admin_only
async def clear_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "album" in context.user_data:
        count = len(context.user_data["album"])
        context.user_data["album"] = []
        await update.message.reply_text(f"🗑️ Cleared {count} videos from queue.")
    else:
        await update.message.reply_text("ℹ️ Queue is already empty.")


@admin_only
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uptime = timedelta(seconds=int(time.time() - state.stats.start_time))
    last_active = datetime.fromtimestamp(state.stats.last_activity).strftime("%Y-%m-%d %H:%M:%S")

    text = (
        f"📊 <b>Bot Statistics</b>\n\n"
        f"📹 Videos processed: <b>{state.stats.total_videos}</b>\n"
        f"📦 Batches sent: <b>{state.stats.total_batches}</b>\n"
        f"🖼️ Photos approved: <b>{state.stats.total_photos}</b>\n"
        f"❌ Photos rejected: <b>{state.stats.rejected_photos}</b>\n"
        f"⏰ Uptime: <b>{uptime}</b>\n"
        f"🕐 Last activity: <b>{last_active}</b>\n"
        f"💾 Cached videos: <b>{len(state.video_cache)}</b>\n"
        f"🔍 Unique checksums: <b>{len(state.checksum_cache)}</b>"
    )
    await update.message.reply_text(text, parse_mode="HTML")


@admin_only
async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Schedule videos to be sent after X minutes"""
    if not context.args:
        await update.message.reply_text("Usage: /schedule 30\nThis will hold videos for 30 minutes before sending.")
        return

    try:
        minutes = int(context.args[0])
        if minutes < 1 or minutes > 1440:  # Max 24 hours
            await update.message.reply_text("❌ Schedule must be between 1 and 1440 minutes (24 hours)")
            return

        chat_id = str(update.effective_chat.id)
        delay_until = time.time() + (minutes * 60)

        context.user_data["schedule_delay"] = delay_until
        await update.message.reply_text(
            f"⏰ Videos will be held for {minutes} minutes\n"
            f"Use /flush to send immediately, /clear to cancel"
        )
    except ValueError:
        await update.message.reply_text("❌ Invalid number")


@admin_only
async def export_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Export statistics to a JSON file"""
    try:
        export_data = {
            "export_time": datetime.now().isoformat(),
            "stats": asdict(state.stats),
            "config": state.to_dict(),
            "admins": ADMIN_IDS
        }

        filename = f"bot_stats_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(filename, 'w') as f:
            json.dump(export_data, f, indent=2)

        await update.message.reply_document(
            document=open(filename, 'rb'),
            caption="📊 Bot statistics export"
        )
        os.remove(filename)  # Clean up
    except Exception as e:
        await update.message.reply_text(f"❌ Export failed: {str(e)}")


# -----------------------------
# Photo Handler (Enhanced)
# -----------------------------
@admin_only
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file_id = update.message.photo[-1].file_id
    file_unique_id = update.message.photo[-1].file_unique_id

    # Check for duplicate photos
    if file_unique_id in state.video_cache:
        await update.message.reply_text("⚠️ Duplicate photo detected.")
        return

    if not state.photo_approval_mode:
        await update.message.reply_photo(file_id)
        state.stats.total_photos += 1
        state.save_config()
        return

    context.user_data["pending_photo"] = file_id
    await update.message.reply_text(
        "🖼️ Photo received.\n"
        "Use /approve or /reject",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data="approve_photo"),
            InlineKeyboardButton("❌ Reject", callback_data="reject_photo")
        ]])
    )


# -----------------------------
# Video Handler (Enhanced with Smart Features)
# -----------------------------
@admin_only
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    state.stats.last_activity = time.time()

    try:
        await message.delete()
    except Exception as e:
        logger.warning(f"Could not delete message: {e}")

    if not message.video:
        return

    video = message.video

    # Create video info object
    video_info = VideoInfo(
        file_id=video.file_id,
        file_unique_id=video.file_unique_id,
        file_size=video.file_size or 0,
        duration=video.duration or 0,
        width=video.width or 0,
        height=video.height or 0,
        timestamp=time.time(),
        checksum=""
    )

    # Generate checksum for content-based dedup
    video_info.checksum = generate_checksum(video_info)

    # Enhanced duplicate detection
    if video.file_unique_id in state.video_cache:
        logger.info(f"Duplicate video detected (ID): {video.file_unique_id}")
        return

    if video_info.checksum in state.checksum_cache:
        logger.info(f"Duplicate video detected (checksum): {video_info.checksum}")
        await update.message.reply_text("⚠️ Duplicate video detected (same content).")
        return

    # Add to caches
    state.video_cache.add(video.file_unique_id)
    state.checksum_cache.add(video_info.checksum)

    # Update stats
    state.stats.total_videos += 1
    state.last_video_time = time.time()
    state.batch_count += 1

    # Check if scheduled delay is active
    schedule_delay = context.user_data.get("schedule_delay", 0)
    if schedule_delay > time.time():
        remaining = int((schedule_delay - time.time()) / 60) + 1
        await update.message.reply_text(
            f"⏰ Video queued ({remaining}min remaining)\n"
            f"📊 Stats: {format_size(video_info.file_size)}, {format_duration(video_info.duration)}"
        )
        # Store in scheduled queue
        chat_id = str(update.effective_chat.id)
        if chat_id not in state.pending_scheduled:
            state.pending_scheduled[chat_id] = []
        state.pending_scheduled[chat_id].append(video_info)
        return

    # Progress indicator
    if state.batch_count % 5 == 0:
        await update.message.reply_text(
            f"📥 Received {state.batch_count} videos…\n"
            f"💾 {format_size(video_info.file_size)} | ⏱️ {format_duration(video_info.duration)}"
        )

    # Initialize album if needed
    if "album" not in context.user_data:
        context.user_data["album"] = []

    # Store video info (not just file_id) for richer captions
    context.user_data["album"].append(video_info)

    # Check if album is full
    if len(context.user_data["album"]) >= state.ALBUM_SIZE:
        await send_album(update, context)


# -----------------------------
# Album Sending (Enhanced with Retry Logic)
# -----------------------------
async def send_album(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: Optional[int] = None):
    album = context.user_data.get("album", [])
    if not album:
        return

    # Random delay for natural sending
    delay = random.uniform(1.5, 3)
    await asyncio.sleep(delay)

    # Prepare media group with optional captions
    media_group = []
    caption_template = context.user_data.get("caption_template", "")

    for idx, video_info in enumerate(album):
        caption = None
        if state.auto_caption_mode and caption_template:
            caption = caption_template.format(
                index=idx+1,
                total=len(album),
                duration=format_duration(video_info.duration),
                size=format_size(video_info.file_size),
                resolution=f"{video_info.width}x{video_info.height}"
            )
        elif state.auto_caption_mode:
            caption = f"📹 Video {idx+1}/{len(album)} | ⏱️ {format_duration(video_info.duration)}"

        media_group.append(InputMediaVideo(
            media=video_info.file_id,
            caption=caption
        ))

    # Send with retry logic
    max_retries = 3
    target_chat = chat_id or update.effective_chat.id

    for attempt in range(max_retries):
        try:
            if update and update.message:
                await update.message.reply_media_group(media_group)
            else:
                await context.bot.send_media_group(target_chat, media_group)

            # Success
            context.user_data["album"] = []
            state.stats.total_batches += 1
            state.save_config()

            # Notify completion
            if update and update.message:
                await update.message.reply_text(
                    f"✅ Sent batch of {len(album)} videos\n"
                    f"📊 Total batches: {state.stats.total_batches}"
                )
            return

        except RetryAfter as e:
            wait_time = e.retry_after + 1
            logger.warning(f"Rate limited, waiting {wait_time}s")
            await asyncio.sleep(wait_time)

        except TimedOut:
            logger.warning(f"Timeout on attempt {attempt + 1}")
            await asyncio.sleep(2)

        except TelegramError as e:
            logger.error(f"Telegram error: {e}")
            if attempt == max_retries - 1:
                if update and update.message:
                    await update.message.reply_text(f"❌ Failed to send album: {str(e)}")
            await asyncio.sleep(1)


async def send_album_to_chat(app, chat_id, album: List[VideoInfo]):
    """Send album to specific chat with enhanced error handling"""
    if not album:
        return

    media_group = []
    for video_info in album:
        media_group.append(InputMediaVideo(media=video_info.file_id))

    max_retries = 3
    for attempt in range(max_retries):
        try:
            await app.bot.send_media_group(chat_id, media_group)
            state.stats.total_batches += 1
            state.save_config()
            return
        except Exception as e:
            logger.error(f"Failed to send album to {chat_id} (attempt {attempt + 1}): {e}")
            await asyncio.sleep(2 ** attempt)  # Exponential backoff


# -----------------------------
# Callback Query Handler
# -----------------------------
@admin_only
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "stats":
        await stats_command(update, context)
    elif data == "settings":
        await settings(update, context)
    elif data == "toggle_photo":
        await toggle_photo_mode(update, context)
    elif data == "toggle_caption":
        await toggle_caption(update, context)
    elif data == "toggle_compression":
        state.compression_mode = not state.compression_mode
        state.save_config()
        status = "ON ✅" if state.compression_mode else "OFF ❌"
        await query.edit_message_text(f"🗜️ Compression is now {status}")
    elif data == "flush":
        await flush(update, context)
    elif data == "help":
        await help_command(update, context)
    elif data == "back_to_main":
        await query.edit_message_text("Main menu:", reply_markup=get_main_keyboard())
    elif data == "approve_photo":
        await approve(update, context)
    elif data == "reject_photo":
        await reject(update, context)
    elif data == "adjust_timeout":
        current = state.FLUSH_TIMEOUT
        keyboard = [
            [InlineKeyboardButton("-10s", callback_data="timeout_-10"),
             InlineKeyboardButton("+10s", callback_data="timeout_+10")],
            [InlineKeyboardButton("-30s", callback_data="timeout_-30"),
             InlineKeyboardButton("+30s", callback_data="timeout_+30")],
            [InlineKeyboardButton("🔙 Back", callback_data="settings")]
        ]
        await query.edit_message_text(
            f"Current timeout: {current}s\nSelect adjustment:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    elif data == "adjust_album":
        current = state.ALBUM_SIZE
        keyboard = [
            [InlineKeyboardButton("-1", callback_data="album_-1"),
             InlineKeyboardButton("+1", callback_data="album_+1")],
            [InlineKeyboardButton("🔙 Back", callback_data="settings")]
        ]
        await query.edit_message_text(
            f"Current album size: {current}\nSelect adjustment:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    elif data.startswith("timeout_"):
        change = int(data.split("_")[1])
        new_value = max(10, min(3600, state.FLUSH_TIMEOUT + change))
        state.FLUSH_TIMEOUT = new_value
        state.save_config()
        await query.edit_message_text(f"✅ Timeout set to {new_value}s")
    elif data.startswith("album_"):
        change = int(data.split("_")[1])
        new_value = max(1, min(10, state.ALBUM_SIZE + change))
        state.ALBUM_SIZE = new_value
        state.save_config()
        await query.edit_message_text(f"✅ Album size set to {new_value}")


# -----------------------------
# Batch Watcher (Enhanced)
# -----------------------------
async def batch_watcher(app):
    now = time.time()

    # Check regular albums
    for chat_id, data in list(app.chat_data.items()):
        album = data.get("album", [])
        if album and now - state.last_video_time >= state.FLUSH_TIMEOUT:
            try:
                await app.bot.send_message(chat_id, "⏰ Batch timeout reached. Sending remaining videos…")
                await send_album_to_chat(app, chat_id, album)
                data["album"] = []
            except Exception as e:
                logger.error(f"Watcher error for {chat_id}: {e}")

    # Check scheduled sends
    for chat_id, videos in list(state.pending_scheduled.items()):
        if videos:
            # Check if any video's schedule has expired
            ready_videos = [v for v in videos if now >= v.timestamp]
            if ready_videos:
                try:
                    await app.bot.send_message(chat_id, f"⏰ Sending {len(ready_videos)} scheduled videos…")
                    await send_album_to_chat(app, int(chat_id), ready_videos)
                    state.pending_scheduled[chat_id] = [v for v in videos if v not in ready_videos]
                except Exception as e:
                    logger.error(f"Scheduled send error for {chat_id}: {e}")


# -----------------------------
# Error Handler
# -----------------------------
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")

    if update and update.message:
        try:
            await update.message.reply_text("⚠️ An error occurred. Please try again.")
        except:
            pass


# -----------------------------
# Post Init
# -----------------------------
async def post_init(app):
    # Load saved config
    state.load_config()

    # Set commands
    commands = [
        BotCommand("start", "Show bot status & menu"),
        BotCommand("help", "Show help"),
        BotCommand("id", "Get your Telegram ID"),
        BotCommand("ping", "Check latency"),
        BotCommand("settings", "Configure bot"),
        BotCommand("stats", "View statistics"),
        BotCommand("toggle_photo_mode", "Toggle photo approval"),
        BotCommand("toggle_caption", "Toggle auto-captions"),
        BotCommand("set_caption", "Set caption template"),
        BotCommand("set_timeout", "Set batch timeout"),
        BotCommand("set_album_size", "Set album size"),
        BotCommand("approve", "Approve photo"),
        BotCommand("reject", "Reject photo"),
        BotCommand("flush", "Send videos now"),
        BotCommand("clear", "Clear queue"),
        BotCommand("schedule", "Schedule delayed send"),
        BotCommand("export", "Export stats"),
    ]

    await app.bot.set_my_commands(commands)

    # Start batch watcher
    app.job_queue.run_repeating(
        lambda ctx: asyncio.create_task(batch_watcher(app)),
        interval=5,
        first=5
    )

    logger.info("✅ Bot initialized with enhanced features")


# -----------------------------
# Main
# -----------------------------
def main():
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing!")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Command handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("id", id_command))
    app.add_handler(CommandHandler("ping", ping_command))
    app.add_handler(CommandHandler("settings", settings))
    app.add_handler(CommandHandler("toggle_photo_mode", toggle_photo_mode))
    app.add_handler(CommandHandler("toggle_caption", toggle_caption))
    app.add_handler(CommandHandler("set_caption", set_caption))
    app.add_handler(CommandHandler("set_timeout", set_timeout))
    app.add_handler(CommandHandler("set_album_size", set_album_size))
    app.add_handler(CommandHandler("approve", approve))
    app.add_handler(CommandHandler("reject", reject))
    app.add_handler(CommandHandler("flush", flush))
    app.add_handler(CommandHandler("clear", clear_queue))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("schedule", schedule_command))
    app.add_handler(CommandHandler("export", export_stats))

    # Callback handler
    app.add_handler(CallbackQueryHandler(button_callback))

    # Media handlers
    app.add_handler(MessageHandler(filters.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # Error handler
    app.add_error_handler(error_handler)

    # Post init
    app.post_init = post_init

    logger.info("🚀 Bot is now polling with enhanced features...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
