# Enhanced Telegram Bot with PostgreSQL

## Features

✅ **PostgreSQL Database Storage** - All data persisted in PostgreSQL (with SQLite fallback)
✅ **Admin Management** - Add/remove admins via commands or database
✅ **Strip Metadata** - Remove sender names and captions by default
✅ **Photo Controls** - Accept/ignore photos with optional approval mode
✅ **Document Controls** - Accept/ignore files with optional approval mode
✅ **Persistent Settings** - All settings survive bot restarts
✅ **Duplicate Detection** - Content-based checksum verification
✅ **Batch Processing** - Auto-group videos into albums

## Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Set Environment Variables

```bash
export BOT_TOKEN="your_telegram_bot_token"
export DATABASE_URL="postgresql://user:password@host:port/database"  # Optional
```

### 3. Run the Bot

```bash
python bot_enhanced.py
```

## Database Setup

### PostgreSQL (Recommended)

1. Create a PostgreSQL database:
```sql
CREATE DATABASE telegram_bot;
```

2. Set the connection URL:
```bash
export DATABASE_URL="postgresql://username:password@localhost:5432/telegram_bot"
```

### SQLite (Fallback)

If no DATABASE_URL is provided, the bot automatically uses SQLite.

## Commands

### Admin Management
- `/addadmin <user_id>` - Add a new admin
- `/removeadmin <user_id>` - Remove an admin
- `/listadmins` - List all admins

### Media Settings
- `/toggle_photos` - Enable/disable photo acceptance
- `/toggle_documents` - Enable/disable document acceptance
- `/toggle_photo_approval` - Enable/disable photo approval mode
- `/toggle_doc_approval` - Enable/disable document approval mode
- `/toggle_strip` - Enable/disable metadata stripping

### Configuration
- `/settings` - Open settings menu
- `/set_timeout <seconds>` - Set batch timeout
- `/set_album_size <1-10>` - Set album size
- `/toggle_caption` - Toggle auto-captions
- `/set_caption <text>` - Set caption template

### Media Control
- `/approve` - Approve pending media
- `/reject` - Reject pending media
- `/flush` - Send pending videos immediately
- `/clear` - Clear queue

### Information
- `/stats` - View statistics
- `/dbinfo` - Database information
- `/export` - Export data to file

## Default Settings

| Setting | Default | Description |
|---------|---------|-------------|
| Strip Metadata | ON | Remove sender names and captions |
| Accept Photos | ON | Process incoming photos |
| Accept Documents | OFF | Ignore documents by default |
| Photo Approval | OFF | Auto-approve photos |
| Document Approval | OFF | Auto-approve documents |
| Batch Timeout | 60s | Time before flushing videos |
| Album Size | 10 | Videos per album |

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| BOT_TOKEN | Yes | Telegram bot token from @BotFather |
| DATABASE_URL | No | PostgreSQL connection string |

## File Structure

```
├── bot_enhanced.py      # Main bot file
├── database.py          # Database module
├── requirements.txt     # Python dependencies
├── bot_settings.json    # Settings backup (auto-created)
├── bot_database.db      # SQLite database (if no PostgreSQL)
└── bot.log             # Log file (auto-created)
```

## Deployment

### Railway

1. Add PostgreSQL plugin to your Railway project
2. Set BOT_TOKEN environment variable
3. Deploy - DATABASE_URL is auto-configured

### Heroku

1. Add Heroku Postgres add-on
2. Set BOT_TOKEN config var
3. Deploy - DATABASE_URL is auto-configured

### Local/VPS

```bash
# Install PostgreSQL
sudo apt install postgresql

# Create database
sudo -u postgres createdb telegram_bot

# Set environment variables
export BOT_TOKEN="your_token"
export DATABASE_URL="postgresql://postgres:password@localhost/telegram_bot"

# Run
python bot_enhanced.py
```

## Security Notes

- Only users in the admin list can use the bot
- Default admin ID: 7599601301 (change in code or add via /addadmin)
- All settings are stored securely in the database
- Media files are not stored, only metadata

## Troubleshooting

**Bot won't start:**
- Check BOT_TOKEN is set correctly
- Verify database connection if using PostgreSQL

**Settings not persisting:**
- Check database permissions
- Look for errors in bot.log

**Database errors:**
- The bot falls back to SQLite if PostgreSQL fails
- Check DATABASE_URL format: `postgresql://user:pass@host:port/db`

## Support

For issues or feature requests, check the bot logs first:
```bash
tail -f bot.log
```
