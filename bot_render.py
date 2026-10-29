"""Admin-only Telegram video collection bot for Render + Supabase.

Environment variables:
  BOT_TOKEN (required)
  ADMIN_ID (required: your Telegram numeric user ID; comma-separated IDs allowed)
  DATABASE_URL (recommended: Supabase PostgreSQL connection string)
  RENDER_EXTERNAL_URL (Render normally provides this automatically)
  PORT (Render normally provides this automatically)

Photos are ignored by default and can be enabled/disabled from the Settings menu.
"""
from __future__ import annotations
import asyncio
import hashlib
import html
import logging
import os
import time
import shlex
from datetime import timedelta
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaVideo, BotCommand
from telegram.error import RetryAfter, TelegramError, TimedOut, NetworkError
from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from threading import Thread
from flask import Flask

health_app = Flask(__name__)

@health_app.get("/")
@health_app.get("/health")
def health():
    return "Bot is running", 200

def run_health_server():
    port = int(os.environ.get("PORT", "10000"))
    health_app.run(
        host="0.0.0.0",
        port=port,
        use_reloader=False
    )

Thread(target=run_health_server, daemon=True).start()
# 2. Now call it to load the .env file
load_dotenv()

from telegram import (
    BotCommand, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaPhoto, InputMediaVideo, Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import (
    ApplicationBuilder, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=[logging.StreamHandler(), RotatingFileHandler("bot.log", maxBytes=2_000_000, backupCount=2)])
logger = logging.getLogger("video_bot")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "10000"))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
ADMIN_IDS = set()
for raw in os.getenv("ADMIN_ID", "").split(","):
    try:
        if raw.strip(): ADMIN_IDS.add(int(raw.strip()))
    except ValueError:
        logger.warning("Ignoring non-numeric value in ADMIN_ID")
# Keep the historical default admin ID for compatibility, while allowing ADMIN_ID overrides.
ADMIN_IDS.add(8976017144)

DEFAULT_SETTINGS = {"accept_photos": False, "album_size": 10, "pause_auto_flush": False}
settings: Dict[str, Any] = {**DEFAULT_SETTINGS, **db.get_all_settings()}
flush_locks: set[tuple[int, int]] = set()


def is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in ADMIN_IDS)

async def deny(update: Update) -> None:
    msg = update.effective_message
    if msg:
        await msg.reply_text("🔒 This bot is private and admin-only.")

async def admin_command(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update):
            await deny(update); return
        try:
            return await func(update, context)
        except Exception:
            logger.exception("Command/handler %s failed", func.__name__)
            if update.effective_message:
                await update.effective_message.reply_text("⚠️ Something went wrong. The error was logged without exposing private credentials.")
    wrapper.__name__ = func.__name__
    return wrapper

def fmt_size(n: int) -> str:
    size = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB": return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"

def fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}" if seconds >= 3600 else f"{seconds // 60}:{seconds % 60:02d}"

def checksum_for(size: int, duration: int, width: int, height: int) -> str:
    return hashlib.sha256(f"{size}:{duration}:{width}:{height}".encode()).hexdigest()

def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Queue status", callback_data="queue_status"), InlineKeyboardButton("📤 Send queued", callback_data="flush")],
        [InlineKeyboardButton("📚 Send all saved videos", callback_data="send_all")],
        [InlineKeyboardButton("🔎 Search", callback_data="search_help"), InlineKeyboardButton("📊 Stats", callback_data="stats")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="settings"), InlineKeyboardButton("❔ Help", callback_data="help")],
    ])

def settings_keyboard() -> InlineKeyboardMarkup:
    photo = bool(settings.get("accept_photos", False))
    pause = bool(settings.get("pause_auto_flush", False))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Photos: {'ON ✅' if photo else 'OFF 🚫'}", callback_data="toggle_photos")],
        [InlineKeyboardButton(f"Auto-send: {'PAUSED ⏸' if pause else 'ON ▶️'}", callback_data="toggle_pause")],
        [InlineKeyboardButton("Batch size: 10 videos", callback_data="noop")],
        [InlineKeyboardButton("🔙 Main menu", callback_data="menu")],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return await deny(update)
    user = update.effective_user
    db.touch_user(user.id, user.username, user.first_name, user.last_name)
    await update.effective_message.reply_text("🎬 <b>Video Vault</b>\n\nSend me videos to save them in your persistent library. Every 10 queued videos are sent as an album. Use Finish/Send queued for a smaller batch. Photos are ignored by default.\n\nChoose an action below or use /help.", parse_mode="HTML", reply_markup=main_keyboard())

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return await deny(update)
    help_text = (
        "<b>Commands</b>\n/start or /menu: open menu\n/collect: show collection instructions\n"
        "/finish: send remaining queued videos\n/get: send queued videos now\n"
        "/search 180: videos at least 180 seconds long\n"
        "/search --exact-duration 180: exactly 180 seconds\n"
        "/search --min-size 10 --max-size 100: size range in MiB\n"
        "/search abc: search IDs\n/stats: counts and progress\n"
        "/settings: toggle photo handling and auto-send\n\n"
        "Videos are stored in the database. Duplicate Telegram file IDs are skipped. Photos are OFF by default."
    )
    await update.effective_message.reply_text(help_text, parse_mode="HTML", reply_markup=main_keyboard())

async def collect_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return await deny(update)
    await update.effective_message.reply_text("📥 Collection is ready. Send videos here. I will queue them persistently and automatically send batches of 10. Send /finish to send a partial batch.", reply_markup=main_keyboard())

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return await deny(update)
    await update.effective_message.reply_text(f"⚙️ <b>Settings</b>\n\nPhotos: {'ON' if settings.get('accept_photos') else 'OFF (default)'}\nAuto-send: {'paused' if settings.get('pause_auto_flush') else 'on'}\nBatch size: 10 videos\n\nSettings are saved to the database.", parse_mode="HTML", reply_markup=settings_keyboard())

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return await deny(update)
    counts = db.get_counts(update.effective_user.id)
    total = db.get_stat("total_videos")
    dupes = db.get_stat("duplicate_videos")
    sent = db.get_stat("sent_videos")
    await update.effective_message.reply_text(f"📊 <b>Video Vault Stats</b>\n\n📚 Saved videos: {counts['videos']}\n📦 Queued now: {counts['queued']}\n📥 Total accepted: {total}\n📤 Videos sent: {sent}\n♻️ Duplicates skipped: {dupes}\n🗄️ Database: {'Supabase/PostgreSQL' if db.use_postgres else 'SQLite (local file)'}", parse_mode="HTML", reply_markup=main_keyboard())

async def queue_status(update: Update, context: ContextTypes.DEFAULT_TYPE, callback: bool = False):
    counts = db.get_counts(update.effective_user.id)
    text = f"📦 <b>Queue status</b>\n\nQueued videos: {counts['queued']}\nSaved library: {counts['videos']}\n\nUse /finish to send the queued items now."
    if callback and update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=main_keyboard())
    else:
        await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=main_keyboard())

async def flush_queue(update: Update, context: ContextTypes.DEFAULT_TYPE, callback: bool = False):
    if not is_admin(update):
        if update.callback_query: await update.callback_query.answer("Access denied", show_alert=True)
        else: await deny(update)
        return
    chat_id, user_id = update.effective_chat.id, update.effective_user.id
    key = (chat_id, user_id)
    if key in flush_locks:
        msg = "A send operation is already running. Please wait."
        if callback: await update.callback_query.answer(msg, show_alert=True)
        else: await update.effective_message.reply_text(msg)
        return
    items = db.get_queue(chat_id, user_id)
    if not items:
        msg = "ℹ️ Your queue is empty. Send me some videos first."
        if callback: await update.callback_query.edit_message_text(msg, reply_markup=main_keyboard())
        else: await update.effective_message.reply_text(msg, reply_markup=main_keyboard())
        return
    flush_locks.add(key)
    status_msg = None
    try:
        if callback:
            await update.callback_query.edit_message_text(f"📤 Sending {len(items)} queued video(s)...")
            status_msg = update.callback_query.message
        else:
            status_msg = await update.effective_message.reply_text(f"📤 Sending {len(items)} queued video(s)...")
        sent_ids: List[str] = []
        failed_ids: List[str] = []
        # Telegram media groups contain 2-10 items. Send singletons individually.
        for offset in range(0, len(items), 10):
            batch = items[offset:offset + 10]
            try:
                if len(batch) == 1:
                    await context.bot.send_video(chat_id=chat_id, video=batch[0]["file_id"])
                else:
                    media = [InputMediaVideo(media=x["file_id"]) for x in batch]
                    await context.bot.send_media_group(chat_id=chat_id, media=media)
                sent_ids.extend(x["file_unique_id"] for x in batch)
            except RetryAfter as exc:
                await asyncio.sleep(float(exc.retry_after) + 1)
                try:
                    if len(batch) == 1: await context.bot.send_video(chat_id=chat_id, video=batch[0]["file_id"])
                    else: await context.bot.send_media_group(chat_id=chat_id, media=[InputMediaVideo(media=x["file_id"]) for x in batch])
                    sent_ids.extend(x["file_unique_id"] for x in batch)
                except TelegramError:
                    failed_ids.extend(x["file_unique_id"] for x in batch)
            except TelegramError:
                logger.exception("Telegram failed to send a batch of %d videos", len(batch))
                # Fall back to individual sends so one invalid video won't block all others.
                for item in batch:
                    try:
                        await context.bot.send_video(chat_id=chat_id, video=item["file_id"])
                        sent_ids.append(item["file_unique_id"])
                    except RetryAfter as exc:
                        await asyncio.sleep(float(exc.retry_after) + 1)
                        try:
                            await context.bot.send_video(chat_id=chat_id, video=item["file_id"])
                            sent_ids.append(item["file_unique_id"])
                        except TelegramError:
                            failed_ids.append(item["file_unique_id"])
                    except TelegramError:
                        failed_ids.append(item["file_unique_id"])
            if offset + 10 < len(items): await asyncio.sleep(1)
        if sent_ids:
            db.remove_queue_items(chat_id, user_id, sent_ids)
            for uid in sent_ids: db.mark_video_sent(uid)
            db.increment_stat("sent_videos", len(sent_ids))
            if len(sent_ids) > 1: db.increment_stat("total_batches")
        # Failed items deliberately stay queued for a retry.
        summary = f"✅ Sent: {len(sent_ids)}\n⚠️ Still queued for retry: {len(failed_ids)}"
        if status_msg:
            try: await context.bot.edit_message_text(summary, chat_id=chat_id, message_id=status_msg.message_id, reply_markup=main_keyboard())
            except TelegramError: await context.bot.send_message(chat_id=chat_id, text=summary, reply_markup=main_keyboard())
    except Exception:
        logger.exception("Queue flush failed")
        if update.effective_message: await update.effective_message.reply_text("⚠️ Sending failed. Items remain in the queue unless already sent; check Render logs.")
    finally:
        flush_locks.discard(key)

async def video_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return await deny(update)
    msg = update.effective_message
    video = msg.video if msg else None
    if not video: return
    uid = video.file_unique_id
    try:
        if db.video_exists(uid):
            db.increment_stat("duplicate_videos")
            await msg.reply_text("♻️ Duplicate video skipped. This file is already saved.")
            return
        checksum = checksum_for(video.file_size or 0, video.duration or 0, video.width or 0, video.height or 0)
        inserted = db.add_video(video.file_id, uid, checksum, video.file_size or 0, video.duration or 0, video.width or 0, video.height or 0, update.effective_chat.id, update.effective_user.id)
        if not inserted:
            db.increment_stat("duplicate_videos")
            await msg.reply_text("♻️ Duplicate video skipped.")
            return
        payload = {"file_id": video.file_id, "file_unique_id": uid, "file_size": video.file_size or 0, "duration": video.duration or 0, "width": video.width or 0, "height": video.height or 0, "checksum": checksum}
        try:
            db.enqueue_video(update.effective_chat.id, update.effective_user.id, payload)
        except Exception:
            db.remove_video_record(uid)
            raise
        db.increment_stat("total_videos")
        counts = db.get_counts(update.effective_user.id)
        # Keep the source message available until an automatic flush has created
        # its status message. This avoids replying to a message already deleted.
        if counts["queued"] >= 10 and not settings.get("pause_auto_flush", False):
            await flush_queue(update, context)
            try: await msg.delete()
            except TelegramError: pass
        else:
            try: await msg.delete()
            except TelegramError: pass
            if counts["queued"] == 1 or counts["queued"] % 10 == 0:
                await context.bot.send_message(chat_id=update.effective_chat.id, text=f"📥 Saved video. Queue: {counts['queued']} | Library: {counts['videos']}")
        if counts["videos"] % 50 == 0:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"📊 Progress: {counts['videos']} videos saved to the library.")
    except Exception:
        logger.exception("Video processing failed (unique ID omitted)")
        await context.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ I couldn't save that video. It has not been intentionally discarded. Please resend it and check the Render logs.")

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return await deny(update)
    if not settings.get("accept_photos", False):
        # Default behavior is to ignore photos quietly, avoiding a reply for every image.
        logger.info("Photo ignored because photo handling is disabled")
        return
    message = update.effective_message
    photo = message.photo[-1]
    try:
        if db.photo_exists(photo.file_unique_id):
            db.increment_stat("duplicate_photos")
            await message.reply_text("♻️ Duplicate photo skipped.")
            return
        inserted = db.add_photo(photo.file_id, photo.file_unique_id, photo.file_size or 0, update.effective_chat.id, update.effective_user.id)
        if not inserted:
            db.increment_stat("duplicate_photos")
            await message.reply_text("♻️ Duplicate photo skipped.")
            return
        db.increment_stat("total_photos")
        await message.reply_text("📷 Photo saved to your persistent library. Photos do not enter the video queue.")
    except Exception:
        logger.exception("Photo processing failed")
        await message.reply_text("⚠️ I could not save this photo. Check Render logs.")

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return await deny(update)
    raw = " ".join(context.args).strip()
    if not raw:
        await update.effective_message.reply_text(
            "🔎 <b>Smart search</b>\n\n"
            "• <code>/search 180</code> = videos at least 180 seconds long\n"
            "• <code>/search --exact-duration 180</code> = exactly 180 seconds\n"
            "• <code>/search --min-duration 600</code> = at least 10 minutes\n"
            "• <code>/search --min-size 10 --max-size 100</code> = size range in MiB\n"
            "• <code>/search abc</code> = search file IDs\n"
            "• Combine ID text and filters, e.g. <code>/search abc --min-size 20</code>",
            parse_mode="HTML")
        return
    try:
        args = shlex.split(raw)
        query_parts: List[str] = []
        filters_map: Dict[str, Any] = {"min_duration": None, "exact_duration": None, "min_size_mb": None, "max_size_mb": None}
        i = 0
        while i < len(args):
            arg = args[i]
            if arg in ("--min-duration", "--exact-duration", "--min-size", "--max-size"):
                if i + 1 >= len(args): raise ValueError(f"{arg} needs a value")
                value = args[i + 1]
                if arg in ("--min-size", "--max-size"):
                    number = float(value)
                    if number < 0: raise ValueError("size values cannot be negative")
                    key = "min_size_mb" if arg == "--min-size" else "max_size_mb"
                else:
                    number = int(value)
                    if number < 0: raise ValueError("duration values cannot be negative")
                    key = "min_duration" if arg == "--min-duration" else "exact_duration"
                filters_map[key] = number
                i += 2
            else:
                query_parts.append(arg)
                i += 1
        query_text = " ".join(query_parts).strip()
        if filters_map["min_size_mb"] is not None and filters_map["max_size_mb"] is not None and filters_map["min_size_mb"] > filters_map["max_size_mb"]:
            raise ValueError("--min-size cannot be larger than --max-size")
        if filters_map["min_duration"] is not None and filters_map["exact_duration"] is not None:
            raise ValueError("Use either --min-duration or --exact-duration, not both")
        if not query_text and all(value is None for value in filters_map.values()):
            raise ValueError("provide a search term or at least one filter")
        videos = db.search_videos_smart(query_text, update.effective_user.id, limit=10, **filters_map)
    except (ValueError, TypeError) as exc:
        await update.effective_message.reply_text(f"⚠️ Search options: {html.escape(str(exc))}\n\nUse /search with no arguments to see examples.", parse_mode="HTML")
        return
    if not videos:
        await update.effective_message.reply_text("No saved videos matched that search.")
        return
    await update.effective_message.reply_text(f"🔎 Found {len(videos)} result(s) shown (maximum 10).")
    for item in videos:
        uid = item["file_unique_id"]
        favorite = db.is_favorite(update.effective_user.id, uid)
        text = (f"🎬 <b>Saved video</b>\nDuration: {fmt_duration(item.get('duration', 0))}\n"
                f"Size: {fmt_size(item.get('file_size', 0))}\nStatus: {html.escape(str(item.get('status', 'saved')))}\n"
                f"Favorite: {'⭐ Yes' if favorite else 'No'}\nID: <code>{html.escape(uid)}</code>")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Send video", callback_data=f"sendone:video:{uid}"),
             InlineKeyboardButton("⭐ Unfavorite" if favorite else "⭐ Favorite", callback_data=f"favorite:{uid}")]
        ])
        await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=kb)

async def send_all_saved_videos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send every saved video for this admin, without relying on the queue."""
    if not is_admin(update): return await deny(update)
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    items = db.get_all_videos(user_id, limit=10000)
    if not items:
        if update.callback_query:
            await update.callback_query.edit_message_text("Your saved video library is empty.", reply_markup=main_keyboard())
        else:
            await update.effective_message.reply_text("Your saved video library is empty.", reply_markup=main_keyboard())
        return
    if update.callback_query:
        await update.callback_query.edit_message_text(
            f"⚠️ <b>Send all saved videos?</b>\n\nThis will send all {len(items)} saved videos, including videos already sent before. It does not delete your library. Continue?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes, send all", callback_data="send_all_confirm"), InlineKeyboardButton("❌ Cancel", callback_data="send_all_cancel")]
            ]))
    else:
        await update.effective_message.reply_text(
            f"⚠️ <b>Send all saved videos?</b>\n\nThis will send all {len(items)} saved videos, including videos already sent before. It does not delete your library. Continue?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes, send all", callback_data="send_all_confirm"), InlineKeyboardButton("❌ Cancel", callback_data="send_all_cancel")]
            ]))

async def confirm_send_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    key = (chat_id, user_id)
    if key in flush_locks:
        await query.answer("Another sending operation is already running.", show_alert=True)
        return
    items = db.get_all_videos(user_id, limit=10000)
    if not items:
        await query.edit_message_text("Your saved video library is empty.", reply_markup=main_keyboard())
        return
    flush_locks.add(key)
    sent_ids: List[str] = []
    failed_ids: List[str] = []
    try:
        status = await query.edit_message_text(f"📤 Starting full-library send: {len(items)} videos...")
        for offset in range(0, len(items), 10):
            batch = items[offset:offset + 10]
            try:
                if len(batch) == 1:
                    await context.bot.send_video(chat_id=chat_id, video=batch[0]["file_id"])
                else:
                    await context.bot.send_media_group(chat_id=chat_id, media=[InputMediaVideo(media=x["file_id"]) for x in batch])
                sent_ids.extend(x["file_unique_id"] for x in batch)
            except RetryAfter as exc:
                await asyncio.sleep(float(exc.retry_after) + 1)
                for item in batch:
                    try:
                        await context.bot.send_video(chat_id=chat_id, video=item["file_id"])
                        sent_ids.append(item["file_unique_id"])
                    except RetryAfter as retry:
                        await asyncio.sleep(float(retry.retry_after) + 1)
                        try:
                            await context.bot.send_video(chat_id=chat_id, video=item["file_id"])
                            sent_ids.append(item["file_unique_id"])
                        except TelegramError:
                            failed_ids.append(item["file_unique_id"])
                    except TelegramError:
                        failed_ids.append(item["file_unique_id"])
            except TelegramError:
                logger.exception("Bulk send batch failed; retrying its videos individually")
                for item in batch:
                    try:
                        await context.bot.send_video(chat_id=chat_id, video=item["file_id"])
                        sent_ids.append(item["file_unique_id"])
                    except RetryAfter as exc:
                        await asyncio.sleep(float(exc.retry_after) + 1)
                        try:
                            await context.bot.send_video(chat_id=chat_id, video=item["file_id"])
                            sent_ids.append(item["file_unique_id"])
                        except TelegramError:
                            failed_ids.append(item["file_unique_id"])
                    except TelegramError:
                        failed_ids.append(item["file_unique_id"])
            if (offset + len(batch)) % 50 == 0 or offset + len(batch) >= len(items):
                try:
                    await context.bot.edit_message_text(
                        f"📤 Full-library send progress\nProcessed: {min(offset + len(batch), len(items))}/{len(items)}\nSent: {len(sent_ids)}\nFailed: {len(failed_ids)}",
                        chat_id=chat_id, message_id=status.message_id)
                except TelegramError:
                    pass
            if offset + 10 < len(items):
                await asyncio.sleep(1)
        if sent_ids:
            db.increment_stat("sent_videos", len(sent_ids))
            db.remove_queue_items(chat_id, user_id, sent_ids)
            for uid in sent_ids:
                db.mark_video_sent(uid)
        try:
            await context.bot.edit_message_text(
                f"✅ <b>Send all finished</b>\n\nLibrary total: {len(items)}\nSent successfully: {len(sent_ids)}\nFailed: {len(failed_ids)}\n\nFailed videos remain in your saved library.",
                chat_id=chat_id, message_id=status.message_id, parse_mode="HTML", reply_markup=main_keyboard())
        except TelegramError:
            await context.bot.send_message(chat_id=chat_id, text=f"Send all finished. Sent: {len(sent_ids)}; failed: {len(failed_ids)}.", reply_markup=main_keyboard())
    except Exception:
        logger.exception("Send all saved videos failed")
        await context.bot.send_message(chat_id=chat_id, text="⚠️ Full-library sending stopped unexpectedly. Your saved library remains intact.", reply_markup=main_keyboard())
    finally:
        flush_locks.discard(key)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(update):
        await query.answer("Access denied", show_alert=True); return
    await query.answer()
    data = query.data or ""
    if data == "menu":
        await query.edit_message_text("🎬 <b>Video Vault</b>\nChoose an action:", parse_mode="HTML", reply_markup=main_keyboard())
    elif data == "help" or data == "search_help":
        await query.edit_message_text("Smart search examples:\n/search 180 = at least 180 seconds\n/search --exact-duration 180 = exactly 180 seconds\n/search --min-size 10 --max-size 100 = size range in MiB\n/search abc = search IDs\n\nSend queued sends only the queue. Send all saved videos resends the entire library.", reply_markup=main_keyboard())
    elif data == "send_all":
        await send_all_saved_videos(update, context)
    elif data == "send_all_confirm":
        await confirm_send_all(update, context)
    elif data == "send_all_cancel":
        await query.edit_message_text("Cancelled. No videos were sent.", reply_markup=main_keyboard())
    elif data.startswith("favorite:"):
        uid = data.split(":", 1)[1]
        item = db.get_video(uid)
        if not item:
            await query.answer("Video not found in library.", show_alert=True)
            return
        now_favorite = db.toggle_favorite(update.effective_user.id, uid)
        label = "⭐ Unfavorite" if now_favorite else "⭐ Favorite"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Send video", callback_data=f"sendone:video:{uid}"), InlineKeyboardButton(label, callback_data=f"favorite:{uid}")]])
        updated_text = (f"🎬 <b>Saved video</b>\nDuration: {fmt_duration(item.get('duration', 0))}\n"
                        f"Size: {fmt_size(item.get('file_size', 0))}\nStatus: {html.escape(str(item.get('status', 'saved')))}\n"
                        f"Favorite: {'⭐ Yes' if now_favorite else 'No'}\nID: <code>{html.escape(uid)}</code>")
        await query.edit_message_text(updated_text, parse_mode="HTML", reply_markup=keyboard)
    elif data == "settings":
        await query.edit_message_text(f"⚙️ Settings\nPhotos: {'ON' if settings.get('accept_photos') else 'OFF'}\nAuto-send: {'paused' if settings.get('pause_auto_flush') else 'on'}", reply_markup=settings_keyboard())
    elif data == "toggle_photos":
        settings["accept_photos"] = not bool(settings.get("accept_photos", False)); db.set_setting("accept_photos", settings["accept_photos"])
        await query.edit_message_text(f"⚙️ Settings\nPhotos are now {'ON' if settings['accept_photos'] else 'OFF'}.\n\nNote: enabling photos currently displays a notice; photo library storage is not implemented in this replacement.", reply_markup=settings_keyboard())
    elif data == "toggle_pause":
        settings["pause_auto_flush"] = not bool(settings.get("pause_auto_flush", False)); db.set_setting("pause_auto_flush", settings["pause_auto_flush"])
        await query.edit_message_text(f"Auto-send is now {'paused' if settings['pause_auto_flush'] else 'on'}.", reply_markup=settings_keyboard())
    elif data == "queue_status":
        await queue_status(update, context, callback=True)
    elif data == "flush":
        await flush_queue(update, context, callback=True)
    elif data == "stats":
        counts = db.get_counts(update.effective_user.id)
        await query.edit_message_text(f"📊 Stats\n\nSaved videos: {counts['videos']}\nSaved photos: {counts.get('photos', 0)}\nQueued: {counts['queued']}\nTotal accepted: {db.get_stat('total_videos')}\nSent: {db.get_stat('sent_videos')}\nDuplicates skipped: {db.get_stat('duplicate_videos')}\nDatabase: {'Supabase/PostgreSQL' if db.use_postgres else 'SQLite local file'}", reply_markup=main_keyboard())
    elif data.startswith("sendone:"):
        parts = data.split(":", 2)
        kind, uid = (parts[1], parts[2]) if len(parts) == 3 else ("video", parts[1])
        item = db.get_photo(uid) if kind == "photo" else db.get_video(uid)
        if not item:
            await query.edit_message_text("That media could not be found in the saved library.", reply_markup=main_keyboard()); return
        try:
            if kind == "photo":
                await context.bot.send_photo(chat_id=update.effective_chat.id, photo=item["file_id"])
                db.increment_stat("sent_photos")
            else:
                await context.bot.send_video(chat_id=update.effective_chat.id, video=item["file_id"])
                db.increment_stat("sent_videos")
            await query.answer("Media sent")
        except TelegramError:
            logger.exception("Could not send searched media")
            await query.answer("Telegram could not send this media. Check logs.", show_alert=True)
    elif data == "noop":
        await query.answer("Batch size is fixed at 10 in this version.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled Telegram update error", exc_info=(type(context.error), context.error, context.error.__traceback__) if context.error else None)

async def post_init(application: Application):
    for user_id in ADMIN_IDS:
        try: db.add_admin(user_id)
        except Exception: logger.exception("Could not sync admin ID %s to database", user_id)
    commands = [BotCommand("start", "Open the bot menu"), BotCommand("menu", "Open the bot menu"), BotCommand("collect", "Start collecting videos"), BotCommand("finish", "Send remaining queued videos"), BotCommand("get", "Send queued videos"), BotCommand("search", "Search saved videos"), BotCommand("stats", "Show statistics"), BotCommand("settings", "Open settings"), BotCommand("help", "Show help")]
    await application.bot.set_my_commands(commands)
    await application.bot.set_my_description("Private video vault. Send videos to save, deduplicate, queue and resend them.")
    logger.info("Bot initialized. Database=%s, admins=%d", "PostgreSQL" if db.use_postgres else "SQLite", len(ADMIN_IDS))

async def main_async():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing. Set it in Render Environment Variables.")
    if not ADMIN_IDS:
        raise RuntimeError("ADMIN_ID is missing. Set your numeric Telegram user ID in Render Environment Variables.")
    builder = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).post_shutdown(lambda app: asyncio.to_thread(db.close))
    application = builder.build()
    application.add_handler(CommandHandler(["start", "menu"], start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("collect", collect_command))
    application.add_handler(CommandHandler("finish", lambda u, c: flush_queue(u, c)))
    application.add_handler(CommandHandler("get", lambda u, c: flush_queue(u, c)))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.VIDEO, video_handler))
    application.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    application.add_error_handler(error_handler)

    if RENDER_URL:
        webhook_url = f"{RENDER_URL}/telegram"
        logger.info("Starting webhook listener on port %s", PORT)
        # Do not log the token or the complete webhook URL.
        await application.initialize()
        await application.start()
        await application.updater.start_webhook(listen="0.0.0.0", port=PORT, url_path="telegram", webhook_url=webhook_url, secret_token=WEBHOOK_SECRET or None, drop_pending_updates=False, allowed_updates=Update.ALL_TYPES)
        try:
            while True: await asyncio.sleep(3600)
        finally:
            await application.updater.stop()
            await application.stop()
            await application.shutdown()
    else:
        # Local development fallback. On Render, RENDER_EXTERNAL_URL is normally present.
        logger.warning("RENDER_EXTERNAL_URL is not set; starting long polling")
        await application.initialize(); await application.start()
        await application.updater.start_polling(drop_pending_updates=False, allowed_updates=Update.ALL_TYPES)
        try:
            while True: await asyncio.sleep(3600)
        finally:
            await application.updater.stop(); await application.stop(); await application.shutdown()

def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.info("Bot stopped")
    except Exception:
        logger.exception("Bot startup or runtime failed")
        raise

if __name__ == "__main__":
    main()
