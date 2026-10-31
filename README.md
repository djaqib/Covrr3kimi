# Video Vault — Shared-Library Telegram Bot

A private, admin-only Telegram bot that acts as a **shared media library**:
all admin accounts access the SAME library of videos and photos. Add files
from one account, tag and search them, and pull them back from any other
account.

## Features

- **Shared library across accounts** — all admins see and share one library
- **PostgreSQL/Supabase storage** (SQLite fallback for local dev)
- **Smart search** — by duration, size, ID, tag, or favorites
- **Tags & captions** — organize videos with tags and custom captions
- **Favorites** — per-user favorites with star toggle
- **Random pick** — `/random` or `/random <tag>` for discovery
- **Auto-flush** — queued videos are sent automatically every N seconds
- **Batch sending** — videos grouped into albums of up to 10
- **Duplicate detection** — skipped automatically
- **Admin management** — `/addadmin`, `/removeadmin`, `/listadmins` at runtime
- **Export** — `/export` downloads a full JSON backup
- **Delete** — `/delete <uid>` removes videos from the library
- **Photo support** — optional, toggleable in settings

## Quick Start

### 1. Install

```bash
pip install -r requirements.txt
```

### 2. Environment variables

```bash
export BOT_TOKEN="your_telegram_bot_token"           # required, from @BotFather
export ADMIN_IDS="123456789,987654321"               # required, comma-separated Telegram user IDs
export DATABASE_URL="postgresql://user:pass@host/db" # optional, Supabase/PostgreSQL
```

### 3. Run

```bash
python bot_render.py
```

## Commands

| Command | Description |
|---|---|
| /start, /menu | Open the bot menu |
| /finish | Send remaining queued videos |
| /search <query> | Smart search (see below) |
| /random [tag] | Send a random video |
| /tags | List all tags |
| /tag <uid> <tag> | Tag a video |
| /untag <uid> <tag> | Remove a tag |
| /caption <uid> <text> | Set a custom caption |
| /favorites | Your favorited videos |
| /delete <uid> | Permanently delete a video |
| /export | Download library backup (JSON) |
| /addadmin <id> | Add an admin |
| /removeadmin <id> | Remove an admin |
| /listadmins | List admins |
| /stats | Statistics |
| /settings | Toggles (photos, auto-send, strip names) |
| /help | Help |

## Search examples

```
/search 180                           # at least 180 seconds
/search --exact-duration 180          # exactly 180 seconds
/search --min-duration 600            # at least 10 minutes
/search --min-size 10 --max-size 100  # size range in MiB
/search --tag workout                 # by tag
/search --favorites                   # only your favorites
/search abc                           # match IDs
/search --tag workout --min-size 20   # combined
```

## Settings (stored in database)

| Setting | Default | Description |
|---|---|---|
| accept_photos | OFF | Save incoming photos |
| pause_auto_flush | OFF | Pause auto-sending |
| strip_metadata | ON | Strip uploader names |
| auto_flush_seconds | 120 | Auto-flush interval |
| album_size | 10 | Videos per album |

## Deployment

### Render (recommended)

1. New Web Service from this repo
2. Set env vars: `BOT_TOKEN`, `ADMIN_IDS`, and (recommended) `DATABASE_URL` from Supabase
3. Render provides `RENDER_EXTERNAL_URL` and `PORT` automatically (webhook mode)

### Railway / Heroku

Same env vars; `DATABASE_URL` is auto-configured if you add their Postgres plugin.

### Local

Without `RENDER_EXTERNAL_URL` the bot runs in long-polling mode. Without
`DATABASE_URL` it uses a local SQLite file (`bot_database.db`).

## File structure

```
├── bot_render.py      # Main bot (webhook + polling)
├── database.py        # PostgreSQL/SQLite layer
├── requirements.txt
├── runtime.txt
├── bot.log            # auto-created
└── bot_database.db    # auto-created (SQLite mode only)
```

## Security notes

- Only IDs in `ADMIN_IDS` can use the bot
- There are **no hardcoded admin IDs** — set them via the env var
- Admins can be added/removed at runtime with `/addadmin` / `/removeadmin`
- Media files are not stored, only Telegram file IDs and metadata
