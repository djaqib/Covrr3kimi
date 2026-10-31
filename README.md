# Video Vault — Shared-Library Telegram Bot

A private, admin-only Telegram bot that acts as a **shared media library**:
all admin accounts access the SAME library of videos and photos. Add files
from one account, tag and search them, and pull them back from any other
account.

## Features

- **Shared library across accounts** — all admins see and share one library
- **PostgreSQL/Supabase storage** (SQLite fallback for local dev)
- **Smart search** — by duration, size, ID, tag, or favorites (10 per page)
- **Tags & captions** — organize videos; captions are sent with the video
- **Favorites** — per-user favorites; ⭐ button on search results AND on every video the bot sends you
- **🗑 Delete button** under every sent video — removes it from the library
- **Random pick** — `/random` or `/random <tag>`, with fav/delete buttons
- **Recent** — `/recent` sends the 40 newest videos, paginated
- **Duplicates** — `/duplicates` finds identical videos (same size+length) and offers to remove copies
- **Clean DB** — `/clean` probes every file_id; dead entries (purged by Telegram) are reported and removable in one tap
- **Pagination everywhere** — large sends page in batches with ➡️/⏹ controls
- **Auto-flush** — queued videos sent automatically every N seconds
- **Admin management** at runtime — `/addadmin`, `/removeadmin`, `/listadmins`
- **Export** — `/export` downloads a full JSON backup

## Setup

```bash
pip install -r requirements.txt
export BOT_TOKEN="your_token"            # from @BotFather
export ADMIN_IDS="123456789,987654321"   # comma-separated Telegram user IDs
export DATABASE_URL="postgresql://..."   # optional, Supabase/PostgreSQL
python bot_render.py
```

## Commands

/start /menu · /finish · /search · /random [tag] · /recent · /duplicates ·
/clean · /tags · /tag · /untag · /caption · /favorites · /delete · /export ·
/addadmin · /removeadmin · /listadmins · /stats · /settings · /help

## Search examples

```
/search 180                           # at least 180 seconds
/search --exact-duration 180
/search --min-size 10 --max-size 100  # MiB range
/search --tag workout
/search --favorites
/search abc                           # match IDs
```

## Deploy (Render)

New Web Service → set BOT_TOKEN, ADMIN_IDS, DATABASE_URL. Render provides
RENDER_EXTERNAL_URL and PORT automatically (webhook mode). For periodic
auto-flush install the job-queue extra:

```
python-telegram-bot[webhooks,job-queue]==21.9
```

## Notes

- Only IDs in ADMIN_IDS can use the bot; no hardcoded IDs anywhere
- Media files stay on Telegram; the DB stores file IDs + metadata
- Very old unused files may be purged by Telegram — `/clean` finds those
- `/duplicates` matches size+duration+resolution; review groups before deleting
