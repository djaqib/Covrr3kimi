"""Shared-library Telegram video vault bot for Render + Supabase.

All admins share ONE library: any admin account can add, search, tag and
retrieve videos/photos added by any other admin account.

Environment variables:
  BOT_TOKEN   (required)
  ADMIN_IDS   (required: comma-separated numeric Telegram user IDs)
  DATABASE_URL (recommended: Supabase PostgreSQL connection string)
  RENDER_EXTERNAL_URL (Render normally provides this automatically)
  PORT (Render normally provides this automatically)
"""
from __future__ import annotations
import asyncio
import hashlib
import html
import io
import json
import logging
import os
import shlex
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaVideo, BotCommand
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from dotenv import load_dotenv
from database import db

load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(), RotatingFileHandler("bot.log", maxBytes=2_000_000, backupCount=2)],
)
logger = logging.getLogger("video_bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "10000"))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

# Admin IDs: ONLY from the ADMIN_IDS env var. No hardcoded IDs.
ADMIN_IDS: set[int] = set()
for raw in os.getenv("ADMIN_IDS", "").split(","):
    try:
        if raw.strip():
            ADMIN_IDS.add(int(raw.strip()))
    except ValueError:
        logger.warning("Ignoring non-numeric value in ADMIN_IDS: %r", raw)

DEFAULT_SETTINGS = {
    "accept_photos": False,
    "album_size": 10,
    "pause_auto_flush": False,
    "auto_flush_seconds": 120,
    "strip_metadata": True,
}
settings: Dict[str, Any] = {**DEFAULT_SETTINGS, **db.get_all_settings()}
flush_locks: set[tuple] = set()
SCAN_LOCKS: set[int] = set()

# Page sizes
SEARCH_PAGE_SIZE = 10      # search results per page (sent as cards)
SEND_PAGE_SIZE = 20        # videos per page for library-wide sends
RECENT_COUNT = 40          # /recent page size
CLEAN_CONCURRENCY = 6      # parallel get_file probes during /clean
CLEAN_CHUNK = 60           # items between progress updates


def is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in ADMIN_IDS)


async def deny(update: Update) -> None:
    msg = update.effective_message
    if msg:
        await msg.reply_text("🔒 This bot is private and admin-only.")


def fmt_size(n: int) -> str:
    size = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}" if seconds >= 3600 else f"{seconds // 60}:{seconds % 60:02d}"


def checksum_for(size: int, duration: int, width: int, height: int) -> str:
    return hashlib.sha256(f"{size}:{duration}:{width}:{height}".encode()).hexdigest()


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------
def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Queue status", callback_data="queue_status"),
         InlineKeyboardButton("📤 Send queued", callback_data="flush")],
        [InlineKeyboardButton("📚 Send all saved videos", callback_data="send_all")],
        [InlineKeyboardButton("🔎 Search", callback_data="search_help"),
         InlineKeyboardButton("🎲 Random", callback_data="random")],
        [InlineKeyboardButton("🕒 Recent", callback_data="recent"),
         InlineKeyboardButton("♻️ Duplicates", callback_data="duplicates")],
        [InlineKeyboardButton("🧹 Clean DB", callback_data="clean"),
         InlineKeyboardButton("🏷 Tags", callback_data="tags")],
        [InlineKeyboardButton("⭐ Favorites", callback_data="favorites"),
         InlineKeyboardButton("📊 Stats", callback_data="stats")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="settings"),
         InlineKeyboardButton("❔ Help", callback_data="help")],
    ])


def settings_keyboard() -> InlineKeyboardMarkup:
    photo = bool(settings.get("accept_photos", False))
    pause = bool(settings.get("pause_auto_flush", False))
    strip = bool(settings.get("strip_metadata", True))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Photos: {'ON ✅' if photo else 'OFF 🚫'}", callback_data="toggle_photos")],
        [InlineKeyboardButton(f"Auto-send: {'PAUSED ⏸' if pause else 'ON ▶️'}", callback_data="toggle_pause")],
        [InlineKeyboardButton(f"Strip names: {'ON ✅' if strip else 'OFF 🚫'}", callback_data="toggle_strip")],
        [InlineKeyboardButton("🔙 Main menu", callback_data="menu")],
    ])


def video_result_keyboard(uid: str, favorite: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Send video", callback_data=f"sendone:video:{uid}"),
         InlineKeyboardButton("⭐ Unfavorite" if favorite else "⭐ Favorite", callback_data=f"favorite:{uid}")],
        [InlineKeyboardButton("🏷 Tag", callback_data=f"tag:{uid}"),
         InlineKeyboardButton("✏️ Caption", callback_data=f"caption:{uid}")],
        [InlineKeyboardButton("🗑 Delete", callback_data=f"del:{uid}")],
    ])


def sent_video_keyboard(uid: str, favorite: bool, user_id: int) -> InlineKeyboardMarkup:
    """Keyboard attached under a video the bot just sent (random / send-one / pager).

    Includes a Favorite toggle and a Delete button. The user_id embedded in the
    fav callback lets each admin keep their own favorite state."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⭐ Unfavorite" if favorite else "⭐ Favorite",
                              callback_data=f"favsent:{uid}:{user_id}")],
        [InlineKeyboardButton("🗑 Delete from library", callback_data=f"delsent:{uid}")],
    ])


# ---------------------------------------------------------------------------
# Pager (pagination for large sends: recent / library / search)
# ---------------------------------------------------------------------------
def pager_text(pager: Dict[str, Any]) -> str:
    label = {"recent": "🕒 Recent videos", "all": "📚 Library", "search": "🔎 Search results"}.get(pager["kind"], "Collection")
    done = min(pager["offset"], pager["total"])
    return f"{label}: {done}/{pager['total']} sent."


def pager_keyboard(pager: Dict[str, Any]) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    if pager["offset"] < pager["total"]:
        rows.append([InlineKeyboardButton(f"➡️ Send next {pager['page_size']}  ({pager['offset']}/{pager['total']})",
                                          callback_data="pgnext")])
    rows.append([InlineKeyboardButton("⏹ Stop", callback_data="pgstop"),
                 InlineKeyboardButton("🔙 Menu", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


async def _fetch_pager_items(pager: Dict[str, Any]) -> List[Dict[str, Any]]:
    kind, off, ps = pager["kind"], pager["offset"], pager["page_size"]
    if kind == "recent":
        return db.get_recent_videos(limit=ps, offset=off)
    if kind == "all":
        return db.get_all_videos(limit=ps, offset=off)
    if kind == "search":
        f = pager["filters"]
        fav_user = pager.get("uid") if f.get("favorites") else None
        return db.search_videos_smart(
            f["q"], limit=ps, offset=off,
            min_duration=f.get("min_duration"), exact_duration=f.get("exact_duration"),
            min_size_mb=f.get("min_size_mb"), max_size_mb=f.get("max_size_mb"),
            tag=f.get("tag"), favorites_of=fav_user)
    return []


async def _send_pager_page(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           pager: Dict[str, Any], items: List[Dict[str, Any]]) -> tuple:
    """Send one page of media. Returns (sent_ids, failed_ids)."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    sent_ids: List[str] = []
    failed_ids: List[str] = []
    for offset in range(0, len(items), 10):
        batch = items[offset:offset + 10]
        try:
            if len(batch) == 1:
                item = batch[0]
                fav = db.is_favorite(user_id, item["file_unique_id"])
                await context.bot.send_video(chat_id=chat_id, video=item["file_id"],
                                             caption=db.get_caption(item["file_unique_id"]),
                                             reply_markup=sent_video_keyboard(item["file_unique_id"], fav, user_id))
            else:
                media = [InputMediaVideo(media=x["file_id"]) for x in batch]
                await context.bot.send_media_group(chat_id=chat_id, media=media)
            sent_ids.extend(x["file_unique_id"] for x in batch)
        except RetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 1)
            s, f = await _send_singles(context.bot, chat_id, batch)
            sent_ids.extend(s); failed_ids.extend(f)
        except TelegramError:
            logger.exception("Telegram failed to send a batch of %d videos", len(batch))
            s, f = await _send_singles(context.bot, chat_id, batch)
            sent_ids.extend(s); failed_ids.extend(f)
        if offset + 10 < len(items):
            await asyncio.sleep(1)
    return sent_ids, failed_ids


async def pager_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    pager = context.chat_data.get("pager")
    if not pager:
        await query.answer("No active collection. Use /recent, /search or Send all first.", show_alert=True)
        return
    if pager.get("owner") != (update.effective_chat.id, update.effective_user.id):
        await query.answer("Only the person who started this collection can continue it.", show_alert=True)
        return
    if pager.get("busy"):
        await query.answer("Already sending, please wait…", show_alert=True)
        return
    items = await _fetch_pager_items(pager)
    if not items:
        await query.answer("No more items.", show_alert=True)
        return
    pager["busy"] = True
    try:
        if pager["kind"] == "search":
            for item in items:
                await _send_search_result(update, context, item)
        else:
            sent, failed = await _send_pager_page(update, context, pager, items)
            for uid in sent:
                db.mark_video_sent(uid)
            if sent:
                db.increment_stat("sent_videos", len(sent))
        pager["offset"] += len(items)
        try:
            await query.edit_message_text(pager_text(pager), reply_markup=pager_keyboard(pager))
        except TelegramError:
            pass
    finally:
        pager["busy"] = False


async def pager_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.chat_data.pop("pager", None)
    await update.callback_query.edit_message_text("⏹ Stopped. Remaining items were not sent.",
                                                  reply_markup=main_keyboard())


# ---------------------------------------------------------------------------
# Basic commands
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny(update)
    user = update.effective_user
    db.touch_user(user.id, user.username, user.first_name, user.last_name)
    await update.effective_message.reply_text(
        "🎬 <b>Video Vault</b> — shared library\n\n"
        "All admin accounts share one library. Send videos/photos here to save them. "
        "Tag them, search, and pull them back from any of your accounts.\n\n"
        "Choose an action below or use /help.",
        parse_mode="HTML", reply_markup=main_keyboard())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny(update)
    help_text = (
        "<b>Commands</b>\n"
        "/start or /menu — open menu\n"
        "/finish — send remaining queued videos\n"
        "/search &lt;query&gt; — smart search (see below)\n"
        "/random [tag] — send a random video\n"
        "/recent — send the 40 most recent videos\n"
        "/duplicates — find identical videos and remove copies\n"
        "/clean — scan DB for dead files, then remove them\n"
        "/tags — list all tags\n"
        "/tag &lt;uid&gt; &lt;tag&gt; — tag a video\n"
        "/untag &lt;uid&gt; &lt;tag&gt; — remove a tag\n"
        "/caption &lt;uid&gt; &lt;text&gt; — set a custom caption\n"
        "/favorites — show your favorited videos\n"
        "/delete &lt;uid&gt; — permanently delete a video\n"
        "/export — download library backup (JSON)\n"
        "/addadmin &lt;id&gt; /removeadmin &lt;id&gt; /listadmins — manage admins\n"
        "/stats — counts and progress\n"
        "/settings — toggles\n\n"
        "<b>Search examples</b>\n"
        "• <code>/search 180</code> — at least 180 seconds\n"
        "• <code>/search --exact-duration 180</code>\n"
        "• <code>/search --min-size 10 --max-size 100</code> — MiB range\n"
        "• <code>/search --tag workout</code>\n"
        "• <code>/search abc</code> — match IDs\n\n"
        "Large sends are paginated: the bot sends a batch, then you tap ➡️ for more."
    )
    await update.effective_message.reply_text(help_text, parse_mode="HTML", reply_markup=main_keyboard())


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny(update)
    await update.effective_message.reply_text(
        f"⚙️ <b>Settings</b>\n\n"
        f"Photos: {'ON' if settings.get('accept_photos') else 'OFF'}\n"
        f"Auto-send: {'paused' if settings.get('pause_auto_flush') else 'on'}\n"
        f"Strip uploader names: {'ON' if settings.get('strip_metadata') else 'OFF'}\n"
        f"Auto-flush interval: {settings.get('auto_flush_seconds', 120)}s",
        parse_mode="HTML", reply_markup=settings_keyboard())


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny(update)
    counts = db.get_counts()
    total = db.get_stat("total_videos")
    dupes = db.get_stat("duplicate_videos")
    sent = db.get_stat("sent_videos")
    size = db.get_total_size()
    await update.effective_message.reply_text(
        f"📊 <b>Video Vault Stats</b> (shared library)\n\n"
        f"📚 Saved videos: {counts['videos']}\n"
        f"📷 Saved photos: {counts['photos']}\n"
        f"💾 Total size: {fmt_size(size)}\n"
        f"📥 Total accepted: {total}\n"
        f"📤 Videos sent: {sent}\n"
        f"♻️ Duplicates skipped: {dupes}\n"
        f"🧹 Dead entries removed: {db.get_stat('cleaned_dead')}\n"
        f"🏷 Tags in use: {len(db.list_tags())}\n"
        f"🗄️ Database: {'Supabase/PostgreSQL' if db.use_postgres else 'SQLite (local file)'}",
        parse_mode="HTML", reply_markup=main_keyboard())


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------
async def queue_status(update: Update, context: ContextTypes.DEFAULT_TYPE, callback: bool = False):
    counts = db.get_counts(update.effective_user.id)
    text = (f"📦 <b>Queue status</b>\n\nQueued videos: {counts['queued']}\n"
            f"Shared library: {counts['videos']} videos / {counts['photos']} photos\n\n"
            f"Use /finish to send the queued items now.")
    if callback and update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=main_keyboard())
    else:
        await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=main_keyboard())


async def _send_singles(bot, chat_id: int, items: List[Dict[str, Any]]) -> tuple:
    sent_ids: List[str] = []
    failed_ids: List[str] = []
    for item in items:
        try:
            await bot.send_video(chat_id=chat_id, video=item["file_id"])
            sent_ids.append(item["file_unique_id"])
        except RetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 1)
            try:
                await bot.send_video(chat_id=chat_id, video=item["file_id"])
                sent_ids.append(item["file_unique_id"])
            except TelegramError:
                failed_ids.append(item["file_unique_id"])
        except TelegramError:
            failed_ids.append(item["file_unique_id"])
    return sent_ids, failed_ids


async def _send_video_batches(bot, chat_id: int, items: List[Dict[str, Any]]) -> tuple:
    """Send videos in batches of up to 10; fall back to singles on failure."""
    sent_ids: List[str] = []
    failed_ids: List[str] = []
    for offset in range(0, len(items), 10):
        batch = items[offset:offset + 10]
        try:
            if len(batch) == 1:
                await bot.send_video(chat_id=chat_id, video=batch[0]["file_id"])
            else:
                media = [InputMediaVideo(media=x["file_id"]) for x in batch]
                await bot.send_media_group(chat_id=chat_id, media=media)
            sent_ids.extend(x["file_unique_id"] for x in batch)
        except RetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 1)
            s, f = await _send_singles(bot, chat_id, batch)
            sent_ids.extend(s); failed_ids.extend(f)
        except TelegramError:
            logger.exception("Telegram failed to send a batch of %d videos", len(batch))
            s, f = await _send_singles(bot, chat_id, batch)
            sent_ids.extend(s); failed_ids.extend(f)
        if offset + 10 < len(items):
            await asyncio.sleep(1)
    return sent_ids, failed_ids


async def flush_queue(update: Update, context: ContextTypes.DEFAULT_TYPE, callback: bool = False):
    if not is_admin(update):
        if update.callback_query:
            await update.callback_query.answer("Access denied", show_alert=True)
        else:
            await deny(update)
        return
    chat_id, user_id = update.effective_chat.id, update.effective_user.id
    key = (chat_id, user_id)
    if key in flush_locks:
        msg = "A send operation is already running. Please wait."
        if callback and update.callback_query:
            await update.callback_query.answer(msg, show_alert=True)
        else:
            await update.effective_message.reply_text(msg)
        return
    items = db.get_queue(chat_id, user_id)
    if not items:
        msg = "ℹ️ Your queue is empty. Send me some videos first."
        if callback and update.callback_query:
            await update.callback_query.edit_message_text(msg, reply_markup=main_keyboard())
        else:
            await update.effective_message.reply_text(msg, reply_markup=main_keyboard())
        return
    flush_locks.add(key)
    status_msg = None
    try:
        if callback and update.callback_query:
            await update.callback_query.edit_message_text(f"📤 Sending {len(items)} queued video(s)...")
            status_msg = update.callback_query.message
        else:
            status_msg = await update.effective_message.reply_text(f"📤 Sending {len(items)} queued video(s)...")
        sent_ids, failed_ids = await _send_video_batches(context.bot, chat_id, items)
        if sent_ids:
            db.remove_queue_items(chat_id, user_id, sent_ids)
            for uid in sent_ids:
                db.mark_video_sent(uid)
            db.increment_stat("sent_videos", len(sent_ids))
            if len(sent_ids) > 1:
                db.increment_stat("total_batches")
        summary = f"✅ Sent: {len(sent_ids)}\n⚠️ Still queued for retry: {len(failed_ids)}"
        if status_msg:
            try:
                await context.bot.edit_message_text(summary, chat_id=chat_id, message_id=status_msg.message_id,
                                                    reply_markup=main_keyboard())
            except TelegramError:
                await context.bot.send_message(chat_id=chat_id, text=summary, reply_markup=main_keyboard())
    except Exception:
        logger.exception("Queue flush failed")
        if update.effective_message:
            await update.effective_message.reply_text("⚠️ Sending failed. Items remain in the queue unless already sent; check Render logs.")
    finally:
        flush_locks.discard(key)


# ---------------------------------------------------------------------------
# Media handlers
# ---------------------------------------------------------------------------
async def video_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny(update)
    msg = update.effective_message
    video = msg.video if msg else None
    if not video:
        return
    uid = video.file_unique_id
    try:
        if db.video_exists(uid):
            db.increment_stat("duplicate_videos")
            await msg.reply_text("♻️ Duplicate video skipped. This file is already in the shared library.")
            return
        checksum = checksum_for(video.file_size or 0, video.duration or 0, video.width or 0, video.height or 0)
        inserted = db.add_video(video.file_id, uid, checksum, video.file_size or 0, video.duration or 0,
                                video.width or 0, video.height or 0, update.effective_chat.id, update.effective_user.id)
        if not inserted:
            db.increment_stat("duplicate_videos")
            await msg.reply_text("♻️ Duplicate video skipped.")
            return
        payload = {"file_id": video.file_id, "file_unique_id": uid, "file_size": video.file_size or 0,
                   "duration": video.duration or 0, "width": video.width or 0, "height": video.height or 0,
                   "checksum": checksum}
        try:
            db.enqueue_video(update.effective_chat.id, update.effective_user.id, payload)
        except Exception:
            db.remove_video_record(uid)
            raise
        db.increment_stat("total_videos")
        counts = db.get_counts(update.effective_user.id)
        batch_size = int(settings.get("album_size", 10))
        if counts["queued"] >= batch_size and not settings.get("pause_auto_flush", False):
            await flush_queue(update, context)
            try:
                await msg.delete()
            except TelegramError:
                pass
        else:
            try:
                await msg.delete()
            except TelegramError:
                pass
            if counts["queued"] == 1 or counts["queued"] % batch_size == 0:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f"📥 Saved to shared library. Your queue: {counts['queued']} | Library: {counts['videos']}")
        if counts["videos"] % 50 == 0:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"📊 Progress: {counts['videos']} videos in the shared library.")
    except Exception:
        logger.exception("Video processing failed (unique ID omitted)")
        await context.bot.send_message(chat_id=update.effective_chat.id,
                                       text="⚠️ I couldn't save that video. Please resend it and check the Render logs.")


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny(update)
    if not settings.get("accept_photos", False):
        logger.info("Photo ignored because photo handling is disabled")
        return
    message = update.effective_message
    photo = message.photo[-1]
    try:
        if db.photo_exists(photo.file_unique_id):
            db.increment_stat("duplicate_photos")
            await message.reply_text("♻️ Duplicate photo skipped.")
            return
        inserted = db.add_photo(photo.file_id, photo.file_unique_id, photo.file_size or 0,
                                update.effective_chat.id, update.effective_user.id)
        if not inserted:
            db.increment_stat("duplicate_photos")
            await message.reply_text("♻️ Duplicate photo skipped.")
            return
        db.increment_stat("total_photos")
        await message.reply_text("📷 Photo saved to the shared library.")
    except Exception:
        logger.exception("Photo processing failed")
        await message.reply_text("⚠️ I could not save this photo. Check Render logs.")


# ---------------------------------------------------------------------------
# Search (shared library, paginated via pager)
# ---------------------------------------------------------------------------
async def _send_search_result(update: Update, context: ContextTypes.DEFAULT_TYPE, item: Dict[str, Any]):
    uid = item["file_unique_id"]
    favorite = db.is_favorite(update.effective_user.id, uid)
    tags = db.get_video_tags(uid)
    caption = db.get_caption(uid)
    text = (f"🎬 <b>Saved video</b>\nDuration: {fmt_duration(item.get('duration', 0))}\n"
            f"Size: {fmt_size(item.get('file_size', 0))}\n"
            f"Status: {html.escape(str(item.get('status', 'saved')))}\n"
            f"Tags: {html.escape(', '.join(tags)) if tags else 'none'}\n"
            f"Caption: {html.escape(caption) if caption else 'none'}\n"
            f"Favorite: {'⭐ Yes' if favorite else 'No'}\n"
            f"ID: <code>{html.escape(uid)}</code>")
    await update.effective_message.reply_text(text, parse_mode="HTML",
                                              reply_markup=video_result_keyboard(uid, favorite))


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny(update)
    raw = " ".join(context.args).strip()
    if not raw:
        await update.effective_message.reply_text(
            "🔎 <b>Smart search</b> (shared library)\n\n"
            "• <code>/search 180</code> — at least 180 seconds\n"
            "• <code>/search --exact-duration 180</code>\n"
            "• <code>/search --min-duration 600</code> — at least 10 minutes\n"
            "• <code>/search --min-size 10 --max-size 100</code> — MiB range\n"
            "• <code>/search --tag workout</code> — by tag\n"
            "• <code>/search --favorites</code> — only your favorites\n"
            "• <code>/search abc</code> — match IDs\n"
            "Combine freely, e.g. <code>/search --tag workout --min-size 20</code>",
            parse_mode="HTML")
        return
    try:
        args = shlex.split(raw)
        query_parts: List[str] = []
        filters_map: Dict[str, Any] = {"min_duration": None, "exact_duration": None,
                                       "min_size_mb": None, "max_size_mb": None,
                                       "tag": None, "favorites": False}
        i = 0
        while i < len(args):
            arg = args[i]
            if arg in ("--min-duration", "--exact-duration", "--min-size", "--max-size", "--tag"):
                if i + 1 >= len(args):
                    raise ValueError(f"{arg} needs a value")
                value = args[i + 1]
                if arg in ("--min-size", "--max-size"):
                    number = float(value)
                    if number < 0:
                        raise ValueError("size values cannot be negative")
                    key = "min_size_mb" if arg == "--min-size" else "max_size_mb"
                    filters_map[key] = number
                elif arg == "--tag":
                    filters_map["tag"] = value
                else:
                    number = int(value)
                    if number < 0:
                        raise ValueError("duration values cannot be negative")
                    key = "min_duration" if arg == "--min-duration" else "exact_duration"
                    filters_map[key] = number
                i += 2
            elif arg == "--favorites":
                filters_map["favorites"] = True
                i += 1
            else:
                query_parts.append(arg)
                i += 1
        query_text = " ".join(query_parts).strip()
        if filters_map["min_size_mb"] is not None and filters_map["max_size_mb"] is not None and filters_map["min_size_mb"] > filters_map["max_size_mb"]:
            raise ValueError("--min-size cannot be larger than --max-size")
        if filters_map["min_duration"] is not None and filters_map["exact_duration"] is not None:
            raise ValueError("use either --min-duration or --exact-duration, not both")
        if not query_text and all(v is None for k, v in filters_map.items() if k != "favorites") and not filters_map["favorites"]:
            raise ValueError("provide a search term or at least one filter")
        user_id = update.effective_user.id if filters_map["favorites"] else None
        videos = db.search_videos_smart(
            query_text, limit=SEARCH_PAGE_SIZE, offset=0,
            min_duration=filters_map["min_duration"], exact_duration=filters_map["exact_duration"],
            min_size_mb=filters_map["min_size_mb"], max_size_mb=filters_map["max_size_mb"],
            tag=filters_map["tag"], favorites_of=user_id)
        total = db.count_videos_smart(
            query_text, min_duration=filters_map["min_duration"], exact_duration=filters_map["exact_duration"],
            min_size_mb=filters_map["min_size_mb"], max_size_mb=filters_map["max_size_mb"],
            tag=filters_map["tag"], favorites_of=user_id)
    except (ValueError, TypeError) as exc:
        await update.effective_message.reply_text(
            f"⚠️ Search options: {html.escape(str(exc))}\n\nUse /search with no arguments to see examples.",
            parse_mode="HTML")
        return
    if not videos:
        await update.effective_message.reply_text("No saved videos matched that search.")
        return
    pager = {"kind": "search", "offset": len(videos), "total": total, "page_size": SEARCH_PAGE_SIZE,
             "filters": {"q": query_text, **filters_map},
             "uid": update.effective_user.id,
             "owner": (update.effective_chat.id, update.effective_user.id)}
    context.chat_data["pager"] = pager
    await update.effective_message.reply_text(f"🔎 {total} result(s).")
    for item in videos:
        await _send_search_result(update, context, item)
    if total > len(videos):
        await update.effective_message.reply_text(pager_text(pager), reply_markup=pager_keyboard(pager))


# ---------------------------------------------------------------------------
# Recent / random / duplicates / tags / captions / favorites / delete
# ---------------------------------------------------------------------------
async def recent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send the RECENT_COUNT most recent videos, paginated if the library is larger."""
    if not is_admin(update):
        return await deny(update)
    chat_id, user_id = update.effective_chat.id, update.effective_user.id
    total = db.count_videos_smart()
    if total == 0:
        await update.effective_message.reply_text("The shared library is empty.")
        return
    items = db.get_recent_videos(limit=RECENT_COUNT, offset=0)
    if not items:
        await update.effective_message.reply_text("No videos yet.")
        return
    status = await update.effective_message.reply_text(f"🕒 Sending the {len(items)} most recent videos…")
    pager = {"kind": "recent", "offset": 0, "total": total, "page_size": RECENT_COUNT,
             "owner": (chat_id, user_id)}
    sent, failed = await _send_pager_page(update, context, pager, items)
    for uid in sent:
        db.mark_video_sent(uid)
    if sent:
        db.increment_stat("sent_videos", len(sent))
    pager["offset"] = len(items)
    context.chat_data["pager"] = pager
    text = pager_text(pager) + (f"\n⚠️ {len(failed)} failed to send." if failed else "")
    try:
        await status.edit_text(text, reply_markup=pager_keyboard(pager))
    except TelegramError:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=pager_keyboard(pager))


async def duplicates_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Find videos with identical checksum (same size+duration+width+height)."""
    if not is_admin(update):
        return await deny(update)
    groups = db.find_duplicates()
    if not groups:
        await update.effective_message.reply_text("♻️ No duplicate videos found in the library.")
        return
    total_extra = sum(len(g) - 1 for g in groups)
    lines = [f"♻️ <b>{len(groups)} duplicate groups</b> — {total_extra} extra copies\n"]
    for idx, g in enumerate(groups[:15], 1):
        m = g[0]
        lines.append(
            f"<b>Group {idx}</b>: {len(g)} copies — {fmt_duration(m['duration'])}, {fmt_size(m['file_size'])}")
        for member in g:
            keep = "✅ keep" if member is g[0] else "🗑 remove"
            lines.append(f"  {keep} <code>{html.escape(member['file_unique_id'])}</code>")
    if len(groups) > 15:
        lines.append(f"…and {len(groups) - 15} more groups.")
    lines.append(f"\nRemove all extra copies (keep oldest in each group)?")
    context.chat_data["dup_groups"] = [[m["file_unique_id"] for m in g] for g in groups]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🗑 Remove {total_extra} extra copies", callback_data="dup_clean")],
        [InlineKeyboardButton("❌ Cancel", callback_data="menu")],
    ])
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)


async def tags_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny(update)
    tags = db.list_tags()
    if not tags:
        await update.effective_message.reply_text("No tags yet. Tag videos with /tag <uid> <tag>.")
        return
    lines = [f"• <code>{html.escape(t)}</code>" for t in tags[:50]]
    await update.effective_message.reply_text(
        f"🏷 <b>Tags in the shared library</b> ({len(tags)})\n\n" + "\n".join(lines),
        parse_mode="HTML", reply_markup=main_keyboard())


async def tag_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny(update)
    if len(context.args) < 2:
        await update.effective_message.reply_text("Usage: /tag <video_uid> <tag>")
        return
    uid, tag = context.args[0], " ".join(context.args[1:])
    if not db.get_video(uid):
        await update.effective_message.reply_text("Video not found in the library.")
        return
    db.add_tag(uid, tag)
    await update.effective_message.reply_text(f"🏷 Tagged <code>{html.escape(uid)}</code> with '{html.escape(tag)}'.",
                                              parse_mode="HTML")


async def untag_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny(update)
    if len(context.args) < 2:
        await update.effective_message.reply_text("Usage: /untag <video_uid> <tag>")
        return
    uid, tag = context.args[0], context.args[1]
    if db.remove_tag(uid, tag):
        await update.effective_message.reply_text(f"Removed tag '{html.escape(tag)}'.", parse_mode="HTML")
    else:
        await update.effective_message.reply_text("That tag was not on this video.")


async def caption_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny(update)
    if len(context.args) < 2:
        await update.effective_message.reply_text("Usage: /caption <video_uid> <text>")
        return
    uid = context.args[0]
    text = " ".join(context.args[1:])
    if not db.get_video(uid):
        await update.effective_message.reply_text("Video not found in the library.")
        return
    db.set_caption(uid, text)
    await update.effective_message.reply_text("✏️ Caption saved.")


async def favorites_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny(update)
    videos = db.search_videos_smart("", limit=20, favorites_of=update.effective_user.id)
    if not videos:
        await update.effective_message.reply_text("You have no favorites yet. Use the ⭐ button on any search result.")
        return
    await update.effective_message.reply_text(f"⭐ Your favorites ({len(videos)} shown, max 20):")
    for item in videos:
        await _send_search_result(update, context, item)


async def random_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny(update)
    tag = context.args[0] if context.args else None
    item = db.get_random_video(tag)
    if not item:
        suffix = f" with tag '{html.escape(tag)}'." if tag else "."
        await update.effective_message.reply_text("No videos found" + suffix, parse_mode="HTML")
        return
    uid = item["file_unique_id"]
    user_id = update.effective_user.id
    try:
        await context.bot.send_video(chat_id=update.effective_chat.id, video=item["file_id"],
                                     caption=db.get_caption(uid),
                                     reply_markup=sent_video_keyboard(uid, db.is_favorite(user_id, uid), user_id))
        db.increment_stat("sent_videos")
    except TelegramError:
        logger.exception("Failed to send random video")
        await update.effective_message.reply_text("⚠️ Telegram could not send that video.")


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny(update)
    if not context.args:
        await update.effective_message.reply_text("Usage: /delete <video_uid>")
        return
    uid = context.args[0]
    if db.delete_video(uid):
        db.increment_stat("deleted_videos")
        await update.effective_message.reply_text("🗑 Video deleted from the shared library.")
    else:
        await update.effective_message.reply_text("Video not found.")


# ---------------------------------------------------------------------------
# Clean: scan the whole database for dead files and optionally remove them
# ---------------------------------------------------------------------------
async def clean_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Probe every file_id with Telegram; entries whose files were purged are
    reported and can be removed from the database with one tap."""
    if not is_admin(update):
        return await deny(update)
    chat_id = update.effective_chat.id
    if chat_id in SCAN_LOCKS:
        await update.effective_message.reply_text("⚠️ A scan is already running. Wait for it to finish.")
        return
    videos = db.get_all_videos(limit=20000)
    photos = db.get_all_photo_refs(limit=20000)
    total = len(videos) + len(photos)
    if total == 0:
        await update.effective_message.reply_text("The database is empty — nothing to scan.")
        return
    SCAN_LOCKS.add(chat_id)
    status_msg = await update.effective_message.reply_text(
        f"🧹 Scanning {total} entries for dead files…\n"
        f"(one Telegram API call per entry; large libraries take a few minutes)")
    dead_videos: List[str] = []
    dead_photos: List[str] = []
    unknown = 0
    sem = asyncio.Semaphore(CLEAN_CONCURRENCY)

    async def check_one(kind: str, item: Dict[str, Any]) -> None:
        nonlocal unknown
        async with sem:
            retries = 2
            while retries > 0:
                retries -= 1
                try:
                    await context.bot.get_file(item["file_id"])
                    return  # file exists on Telegram
                except BadRequest:
                    (dead_photos if kind == "photo" else dead_videos).append(item["file_unique_id"])
                    return
                except RetryAfter as exc:
                    await asyncio.sleep(float(exc.retry_after) + 1)
                except TelegramError:
                    unknown += 1
                    return
            unknown += 1

    processed = 0
    all_items = [("video", v) for v in videos] + [("photo", p) for p in photos]
    try:
        for i in range(0, total, CLEAN_CHUNK):
            chunk = all_items[i:i + CLEAN_CHUNK]
            await asyncio.gather(*(check_one(k, it) for k, it in chunk))
            processed += len(chunk)
            try:
                await status_msg.edit_text(
                    f"🧹 Scanning… {processed}/{total}\n"
                    f"Dead found so far: {len(dead_videos) + len(dead_photos)}")
            except TelegramError:
                pass
            await asyncio.sleep(0.3)
    finally:
        SCAN_LOCKS.discard(chat_id)

    dead_total = len(dead_videos) + len(dead_photos)
    if dead_total == 0:
        msg = f"✅ Scan complete: {processed} entries checked, no dead files found."
        if unknown:
            msg += f"\n⚠️ {unknown} entries could not be verified (Telegram errors) — run /clean again later."
        await status_msg.edit_text(msg, reply_markup=main_keyboard())
        return
    context.chat_data["clean_dead_videos"] = dead_videos
    context.chat_data["clean_dead_photos"] = dead_photos
    msg = (f"🧹 Scan complete.\nChecked: {processed}\n"
           f"🗑 Dead videos: {len(dead_videos)}\n"
           f"🗑 Dead photos: {len(dead_photos)}")
    if unknown:
        msg += f"\n⚠️ Unverified (kept in DB): {unknown}"
    msg += "\n\nRemove the dead entries from the database?"
    await status_msg.edit_text(
        msg,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(f"🗑 Remove {dead_total} dead entries", callback_data="clean_remove"),
            InlineKeyboardButton("❌ Keep them", callback_data="clean_cancel"),
        ]]))


# ---------------------------------------------------------------------------
# Admin management
# ---------------------------------------------------------------------------
async def addadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny(update)
    if not context.args:
        await update.effective_message.reply_text("Usage: /addadmin <user_id>")
        return
    try:
        new_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("user_id must be a number.")
        return
    ADMIN_IDS.add(new_id)
    db.add_admin(new_id)
    await update.effective_message.reply_text(f"✅ Added admin <code>{new_id}</code>.", parse_mode="HTML")


async def removeadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny(update)
    if not context.args:
        await update.effective_message.reply_text("Usage: /removeadmin <user_id>")
        return
    try:
        rid = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("user_id must be a number.")
        return
    if rid == update.effective_user.id:
        await update.effective_message.reply_text("You cannot remove yourself.")
        return
    ADMIN_IDS.discard(rid)
    db.remove_admin(rid)
    await update.effective_message.reply_text(f"✅ Removed admin <code>{rid}</code>.", parse_mode="HTML")


async def listadmins_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny(update)
    admins = db.list_admins()
    if not admins:
        await update.effective_message.reply_text("No admins in the database.")
        return
    lines = [f"• <code>{a['user_id']}</code> {html.escape(a.get('username') or a.get('first_name') or '')}"
             for a in admins]
    await update.effective_message.reply_text(f"👑 <b>Admins</b>\n\n" + "\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny(update)
    data = db.export_library()
    payload = json.dumps(data, indent=2, default=str)
    if len(payload) > 45_000_000:
        await update.effective_message.reply_text("⚠️ Library too large to export via Telegram.")
        return
    await update.effective_message.reply_document(
        document=io.BytesIO(payload.encode("utf-8")),
        filename="video_vault_backup.json",
        caption=f"Backup: {len(data['videos'])} videos, {len(data['photos'])} photos, {len(data['tags'])} tags")


# ---------------------------------------------------------------------------
# Send all saved videos (paginated)
# ---------------------------------------------------------------------------
async def send_all_saved_videos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny(update)
    total = db.count_videos_smart()
    if total == 0:
        text = "The shared library is empty."
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=main_keyboard())
        else:
            await update.effective_message.reply_text(text, reply_markup=main_keyboard())
        return
    text = (f"⚠️ <b>Send all saved videos?</b>\n\nThis sends the whole shared library "
            f"({total} videos) in pages of {SEND_PAGE_SIZE}. Tap ➡️ to continue after each page. "
            f"Nothing is deleted. Continue?")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, start sending", callback_data="send_all_confirm"),
         InlineKeyboardButton("❌ Cancel", callback_data="send_all_cancel")]])
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def confirm_send_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    key = (chat_id, user_id)
    if key in flush_locks:
        await query.answer("Another sending operation is already running.", show_alert=True)
        return
    total = db.count_videos_smart()
    if total == 0:
        await query.edit_message_text("The shared library is empty.", reply_markup=main_keyboard())
        return
    first = db.get_all_videos(limit=SEND_PAGE_SIZE, offset=0)
    flush_locks.add(key)
    try:
        status = await query.edit_message_text(f"📤 Sending first {len(first)} of {total} videos…")
        pager = {"kind": "all", "offset": 0, "total": total, "page_size": SEND_PAGE_SIZE,
                 "owner": (chat_id, user_id)}
        sent, failed = await _send_pager_page(update, context, pager, first)
        for uid in sent:
            db.mark_video_sent(uid)
        if sent:
            db.increment_stat("sent_videos", len(sent))
            db.remove_queue_items(chat_id, user_id, sent)
        pager["offset"] = len(first)
        context.chat_data["pager"] = pager
        text = pager_text(pager) + (f"\n⚠️ {len(failed)} failed to send." if failed else "")
        try:
            await status.edit_text(text, reply_markup=pager_keyboard(pager))
        except TelegramError:
            await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=pager_keyboard(pager))
    finally:
        flush_locks.discard(key)


# ---------------------------------------------------------------------------
# Periodic auto-flush
# ---------------------------------------------------------------------------
async def auto_flush_job(context: ContextTypes.DEFAULT_TYPE):
    if settings.get("pause_auto_flush", False):
        return
    for admin_id in list(ADMIN_IDS):
        key = (admin_id, admin_id)
        if key in flush_locks:
            continue
        items = db.get_queue(admin_id, admin_id)
        if not items:
            continue
        flush_locks.add(key)
        try:
            sent_ids, failed_ids = await _send_video_batches(context.bot, admin_id, items)
            if sent_ids:
                db.remove_queue_items(admin_id, admin_id, sent_ids)
                for uid in sent_ids:
                    db.mark_video_sent(uid)
                db.increment_stat("sent_videos", len(sent_ids))
                await context.bot.send_message(chat_id=admin_id,
                                               text=f"⏰ Auto-flush: sent {len(sent_ids)} queued video(s)."
                                                    + (f" {len(failed_ids)} failed and remain queued." if failed_ids else ""))
        except Exception:
            logger.exception("Auto-flush failed for admin %s", admin_id)
        finally:
            flush_locks.discard(key)


# ---------------------------------------------------------------------------
# Callback handler
# ---------------------------------------------------------------------------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(update):
        await query.answer("Access denied", show_alert=True)
        return
    await query.answer()
    data = query.data or ""

    if data == "menu":
        await query.edit_message_text("🎬 <b>Video Vault</b> — shared library\nChoose an action:",
                                      parse_mode="HTML", reply_markup=main_keyboard())
    elif data == "help" or data == "search_help":
        await query.edit_message_text(
            "Smart search examples:\n/search 180 = at least 180 seconds\n"
            "/search --exact-duration 180\n/search --min-size 10 --max-size 100 = MiB range\n"
            "/search --tag workout = by tag\n/search --favorites = your favorites\n"
            "/search abc = match IDs\n\n"
            "Other commands: /recent, /duplicates, /random [tag], /tags, /favorites, /delete <uid>, /clean, /export, /addadmin <id>",
            reply_markup=main_keyboard())
    elif data == "send_all":
        await send_all_saved_videos(update, context)
    elif data == "send_all_confirm":
        await confirm_send_all(update, context)
    elif data == "send_all_cancel":
        await query.edit_message_text("Cancelled. No videos were sent.", reply_markup=main_keyboard())
    elif data == "random":
        await random_command(update, context)
    elif data == "recent":
        await recent_command(update, context)
    elif data == "duplicates":
        await duplicates_command(update, context)
    elif data == "dup_clean":
        groups = context.chat_data.pop("dup_groups", [])
        removed = 0
        for group in groups:
            for uid in group[1:]:  # keep the oldest (first) member
                if db.delete_video(uid):
                    removed += 1
        db.increment_stat("deleted_videos", removed)
        await query.edit_message_text(f"🗑 Removed {removed} duplicate videos (oldest copy of each group kept).",
                                      reply_markup=main_keyboard())
    elif data == "clean":
        await clean_command(update, context)
    elif data == "pgnext":
        await pager_next(update, context)
    elif data == "pgstop":
        await pager_stop(update, context)
    elif data == "clean_remove":
        dv = context.chat_data.pop("clean_dead_videos", [])
        dp = context.chat_data.pop("clean_dead_photos", [])
        removed_v = sum(1 for uid in dv if db.delete_video(uid))
        removed_p = sum(1 for uid in dp if db.delete_photo(uid))
        db.increment_stat("cleaned_dead", removed_v + removed_p)
        await query.edit_message_text(
            f"🗑 Database cleaned: removed {removed_v} dead videos and {removed_p} dead photos.",
            reply_markup=main_keyboard())
    elif data == "clean_cancel":
        context.chat_data.pop("clean_dead_videos", None)
        context.chat_data.pop("clean_dead_photos", None)
        await query.edit_message_text("Cancelled. Dead entries were kept in the database.",
                                      reply_markup=main_keyboard())
    elif data == "tags":
        await tags_command(update, context)
    elif data == "favorites":
        await favorites_command(update, context)
    elif data.startswith("favsent:"):
        parts = data.split(":")
        uid = parts[1]
        kb_owner = int(parts[2]) if len(parts) > 2 else update.effective_user.id
        item = db.get_video(uid)
        if not item:
            await query.answer("Video no longer in the library.", show_alert=True)
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except TelegramError:
                pass
            return
        now_fav = db.toggle_favorite(update.effective_user.id, uid)
        try:
            await query.edit_message_reply_markup(
                reply_markup=sent_video_keyboard(uid, now_fav, kb_owner))
        except TelegramError:
            pass
        await query.answer("⭐ Added to favorites" if now_fav else "Removed from favorites")
    elif data.startswith("delsent:"):
        uid = data.split(":", 1)[1]
        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Confirm delete from library", callback_data=f"delsentconfirm:{uid}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"delsentback:{uid}"),
            ]]))
    elif data.startswith("delsentconfirm:"):
        uid = data.split(":", 1)[1]
        removed = db.delete_video(uid)
        db.increment_stat("deleted_videos", 1 if removed else 0)
        try:
            await query.message.delete()
        except TelegramError:
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except TelegramError:
                pass
        await query.answer("🗑 Deleted from library" if removed else "Already removed")
    elif data.startswith("delsentback:"):
        uid = data.split(":", 1)[1]
        user_id = update.effective_user.id
        try:
            await query.edit_message_reply_markup(
                reply_markup=sent_video_keyboard(uid, db.is_favorite(user_id, uid), user_id))
        except TelegramError:
            pass
    elif data.startswith("favorite:"):
        uid = data.split(":", 1)[1]
        item = db.get_video(uid)
        if not item:
            await query.answer("Video not found in library.", show_alert=True)
            return
        now_favorite = db.toggle_favorite(update.effective_user.id, uid)
        tags = db.get_video_tags(uid)
        caption = db.get_caption(uid)
        updated_text = (f"🎬 <b>Saved video</b>\nDuration: {fmt_duration(item.get('duration', 0))}\n"
                        f"Size: {fmt_size(item.get('file_size', 0))}\n"
                        f"Status: {html.escape(str(item.get('status', 'saved')))}\n"
                        f"Tags: {html.escape(', '.join(tags)) if tags else 'none'}\n"
                        f"Caption: {html.escape(caption) if caption else 'none'}\n"
                        f"Favorite: {'⭐ Yes' if now_favorite else 'No'}\n"
                        f"ID: <code>{html.escape(uid)}</code>")
        await query.edit_message_text(updated_text, parse_mode="HTML",
                                      reply_markup=video_result_keyboard(uid, now_favorite))
    elif data.startswith("tag:"):
        uid = data.split(":", 1)[1]
        context.user_data["awaiting_tag_for"] = uid
        await query.edit_message_text(
            f"🏷 Send the tag for <code>{html.escape(uid)}</code> as a normal message now.",
            parse_mode="HTML")
    elif data.startswith("caption:"):
        uid = data.split(":", 1)[1]
        context.user_data["awaiting_caption_for"] = uid
        await query.edit_message_text(
            f"✏️ Send the caption for <code>{html.escape(uid)}</code> as a normal message now.",
            parse_mode="HTML")
    elif data.startswith("del:"):
        uid = data.split(":", 1)[1]
        await query.edit_message_text(
            f"🗑 Really delete <code>{html.escape(uid)}</code> from the shared library? This cannot be undone.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes, delete", callback_data=f"delconfirm:{uid}"),
                 InlineKeyboardButton("❌ Cancel", callback_data="menu")]]))
    elif data.startswith("delconfirm:"):
        uid = data.split(":", 1)[1]
        if db.delete_video(uid):
            db.increment_stat("deleted_videos")
            await query.edit_message_text("🗑 Video deleted from the shared library.", reply_markup=main_keyboard())
        else:
            await query.edit_message_text("Video not found.", reply_markup=main_keyboard())
    elif data == "settings":
        await query.edit_message_text(
            f"⚙️ Settings\nPhotos: {'ON' if settings.get('accept_photos') else 'OFF'}\n"
            f"Auto-send: {'paused' if settings.get('pause_auto_flush') else 'on'}\n"
            f"Strip uploader names: {'ON' if settings.get('strip_metadata') else 'OFF'}",
            reply_markup=settings_keyboard())
    elif data == "toggle_photos":
        settings["accept_photos"] = not bool(settings.get("accept_photos", False))
        db.set_setting("accept_photos", settings["accept_photos"])
        await query.edit_message_text(f"Photos are now {'ON' if settings['accept_photos'] else 'OFF'}.",
                                      reply_markup=settings_keyboard())
    elif data == "toggle_pause":
        settings["pause_auto_flush"] = not bool(settings.get("pause_auto_flush", False))
        db.set_setting("pause_auto_flush", settings["pause_auto_flush"])
        await query.edit_message_text(f"Auto-send is now {'paused' if settings['pause_auto_flush'] else 'on'}.",
                                      reply_markup=settings_keyboard())
    elif data == "toggle_strip":
        settings["strip_metadata"] = not bool(settings.get("strip_metadata", True))
        db.set_setting("strip_metadata", settings["strip_metadata"])
        await query.edit_message_text(
            f"Stripping uploader names is now {'ON' if settings['strip_metadata'] else 'OFF'}.",
            reply_markup=settings_keyboard())
    elif data == "queue_status":
        await queue_status(update, context, callback=True)
    elif data == "flush":
        await flush_queue(update, context, callback=True)
    elif data == "stats":
        counts = db.get_counts()
        await query.edit_message_text(
            f"📊 Stats (shared library)\n\nSaved videos: {counts['videos']}\n"
            f"Saved photos: {counts['photos']}\nQueued: {counts['queued']}\n"
            f"Total accepted: {db.get_stat('total_videos')}\nSent: {db.get_stat('sent_videos')}\n"
            f"Duplicates skipped: {db.get_stat('duplicate_videos')}\n"
            f"Dead removed: {db.get_stat('cleaned_dead')}\n"
            f"Database: {'Supabase/PostgreSQL' if db.use_postgres else 'SQLite local file'}",
            reply_markup=main_keyboard())
    elif data.startswith("sendone:"):
        parts = data.split(":", 2)
        kind, uid = (parts[1], parts[2]) if len(parts) == 3 else ("video", parts[1])
        item = db.get_photo(uid) if kind == "photo" else db.get_video(uid)
        if not item:
            await query.edit_message_text("That media could not be found in the saved library.",
                                          reply_markup=main_keyboard())
            return
        user_id = update.effective_user.id
        try:
            if kind == "photo":
                await context.bot.send_photo(chat_id=update.effective_chat.id, photo=item["file_id"])
                db.increment_stat("sent_photos")
            else:
                await context.bot.send_video(chat_id=update.effective_chat.id, video=item["file_id"],
                                             caption=db.get_caption(uid),
                                             reply_markup=sent_video_keyboard(uid, db.is_favorite(user_id, uid), user_id))
                db.increment_stat("sent_videos")
            await query.answer("Media sent")
        except TelegramError:
            logger.exception("Could not send searched media")
            await query.answer("Telegram could not send this media. Check logs.", show_alert=True)
    elif data == "noop":
        await query.answer("Batch size is fixed at 10 in this version.")


# ---------------------------------------------------------------------------
# Text message router (for tag/caption prompts)
# ---------------------------------------------------------------------------
async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny(update)
    tag_target = context.user_data.pop("awaiting_tag_for", None)
    if tag_target:
        tag = update.effective_message.text.strip()
        if tag:
            db.add_tag(tag_target, tag)
            await update.effective_message.reply_text(f"🏷 Tagged with '{html.escape(tag)}'.", parse_mode="HTML")
        return
    cap_target = context.user_data.pop("awaiting_caption_for", None)
    if cap_target:
        caption = update.effective_message.text.strip()
        db.set_caption(cap_target, caption)
        await update.effective_message.reply_text("✏️ Caption saved.")
        return
    await update.effective_message.reply_text("I only understand videos/photos and commands. See /help.",
                                              reply_markup=main_keyboard())


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled Telegram update error",
                 exc_info=(type(context.error), context.error, context.error.__traceback__) if context.error else None)


async def post_init(application: Application):
    for user_id in ADMIN_IDS:
        try:
            db.add_admin(user_id)
        except Exception:
            logger.exception("Could not sync admin ID %s to database", user_id)
    commands = [
        BotCommand("start", "Open the bot menu"),
        BotCommand("menu", "Open the bot menu"),
        BotCommand("finish", "Send remaining queued videos"),
        BotCommand("search", "Search saved videos"),
        BotCommand("random", "Send a random video"),
        BotCommand("recent", "Send the 40 most recent videos"),
        BotCommand("duplicates", "Find duplicate videos and remove copies"),
        BotCommand("clean", "Scan DB for dead files and remove them"),
        BotCommand("tags", "List all tags"),
        BotCommand("favorites", "Your favorited videos"),
        BotCommand("delete", "Delete a video from the library"),
        BotCommand("export", "Download library backup"),
        BotCommand("addadmin", "Add an admin"),
        BotCommand("removeadmin", "Remove an admin"),
        BotCommand("listadmins", "List admins"),
        BotCommand("stats", "Show statistics"),
        BotCommand("settings", "Open settings"),
        BotCommand("help", "Show help"),
    ]
    await application.bot.set_my_commands(commands)
    await application.bot.set_my_description(
        "Private shared video vault for admins. Save, tag, search and resend videos across accounts.")
    if application.job_queue:
        interval = max(30, int(settings.get("auto_flush_seconds", 120)))
        application.job_queue.run_repeating(auto_flush_job, interval=interval, first=interval)
        logger.info("Auto-flush enabled every %ss", interval)
    else:
        logger.warning("python-telegram-bot[job-queue] not installed; periodic auto-flush disabled. "
                       "Add job-queue to the pip extras to enable it.")
    logger.info("Bot initialized. Database=%s, admins=%d",
                "PostgreSQL" if db.use_postgres else "SQLite", len(ADMIN_IDS))


async def main_async():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing. Set it in Render Environment Variables.")
    if not ADMIN_IDS:
        raise RuntimeError("ADMIN_IDS is missing. Set your numeric Telegram user ID(s) in Render Environment Variables.")
    builder = (ApplicationBuilder()
               .token(BOT_TOKEN)
               .post_init(post_init)
               .post_shutdown(lambda app: asyncio.to_thread(db.close)))
    application = builder.build()
    application.add_handler(CommandHandler(["start", "menu"], start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("random", random_command))
    application.add_handler(CommandHandler("recent", recent_command))
    application.add_handler(CommandHandler("duplicates", duplicates_command))
    application.add_handler(CommandHandler("clean", clean_command))
    application.add_handler(CommandHandler("tags", tags_command))
    application.add_handler(CommandHandler("tag", tag_command))
    application.add_handler(CommandHandler("untag", untag_command))
    application.add_handler(CommandHandler("caption", caption_command))
    application.add_handler(CommandHandler("favorites", favorites_command))
    application.add_handler(CommandHandler("delete", delete_command))
    application.add_handler(CommandHandler("export", export_command))
    application.add_handler(CommandHandler("addadmin", addadmin_command))
    application.add_handler(CommandHandler("removeadmin", removeadmin_command))
    application.add_handler(CommandHandler("listadmins", listadmins_command))
    application.add_handler(CommandHandler(["finish", "get"], flush_queue))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.VIDEO, video_handler))
    application.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    application.add_error_handler(error_handler)

    if RENDER_URL:
        webhook_url = f"{RENDER_URL}/telegram"
        logger.info("Starting webhook listener on port %s", PORT)
        await application.initialize()
        await application.start()
        await application.updater.start_webhook(listen="0.0.0.0", port=PORT, url_path="telegram",
                                                webhook_url=webhook_url,
                                                secret_token=WEBHOOK_SECRET or None,
                                                drop_pending_updates=False,
                                                allowed_updates=Update.ALL_TYPES)
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await application.updater.stop()
            await application.stop()
            await application.shutdown()
    else:
        logger.warning("RENDER_EXTERNAL_URL is not set; starting long polling")
        await application.initialize()
        await application.start()
        await application.updater.start_polling(drop_pending_updates=False, allowed_updates=Update.ALL_TYPES)
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await application.updater.stop()
            await application.stop()
            await application.shutdown()


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
