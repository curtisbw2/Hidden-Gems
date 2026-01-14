# Hidden Gems Research – The Gem Vault Discord Bot

A production-ready Discord bot for managing an options trading community, featuring onboarding, role management, premium verification flows, and daily price alerts.

## Features

- **Onboarding**: Automatic free role assignment, welcome DMs, interactive `/start` command
- **Role Management**: Mod/admin commands for granting/revoking roles, user lookup
- **Premium Verification**: Two verification methods:
  - **Mod Queue**: Submit proof via `/verify_premium` for manual review
  - **Email Linking**: Link Substack email via `/link_email` + `/confirm_code` (no screenshot required)
- **CSV Import**: Import paid subscribers from Substack exports, automatic role sync
- **Price Alerts**: Daily monitoring of ticker price changes (±10% threshold)
- **Privacy-First**: Emails are hashed (SHA-256), no raw images stored

## Requirements

- Python 3.11+
- Discord Bot Token
- SendGrid API Key (for email OTP)
- Discord Server with appropriate roles and channels

## Setup

### 1. Create Discord Bot

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Click "New Application" and name it
3. Go to "Bot" section:
   - Click "Add Bot"
   - **IMPORTANT**: Under "Privileged Gateway Intents", enable:
     - ✅ **SERVER MEMBERS INTENT** (required for role assignment on member join)
     - ✅ **MESSAGE CONTENT INTENT** (required for reading message attachments in verification)
   - **Save Changes** after enabling intents
   - Copy the bot token (you'll need this - click "Reset Token" if needed)
4. Go to "OAuth2" → "URL Generator":
   - Select scopes: `bot`, `applications.commands`
   - Select bot permissions:
     - Manage Roles
     - Send Messages
     - Read Message History
     - Attach Files
     - Embed Links
   - Copy the generated invite URL

**⚠️ Note**: If you see "PrivilegedIntentsRequired" error, you MUST enable the intents in the Developer Portal. The bot cannot function without these intents.

### 2. Invite Bot to Server

1. Use the invite URL from step 1
2. Select your server and authorize
3. **Important**: Ensure the bot's role is positioned **above** the Premium Member role in Server Settings → Roles

### 3. Create Roles and Channels

Create the following in your Discord server:

**Roles:**
- `Free Member` (or configure via env)
- `Premium Member` (or configure via env)
- `Admin` (or configure via env)
- `Mod` (or configure via env)

**Channels:**
- `#verify` - Public verification channel
- `#verify-queue` - Private mod queue channel
- `#bot-logs` - Private log channel
- `#alerts` - Alerts channel

**Note**: Channel and role names can be customized via environment variables. You can also use IDs directly.

### 4. Install Dependencies

```bash
pip install -r requirements.txt
```

### 5. Configure Environment Variables

Create a `.env` file in the project root (see `.env.example` for template):

```env
# Required
DISCORD_TOKEN=your_bot_token_here
GUILD_ID=your_guild_id_here

# Email (required for email linking)
SENDGRID_API_KEY=your_sendgrid_api_key
FROM_EMAIL=noreply@yourdomain.com

# Database
# For local dev (SQLite):
DB_PATH=data/bot.db
# For production (Postgres on Railway):
# DATABASE_URL is automatically set when you add Postgres service
# DATABASE_URL=postgresql://user:password@host:port/database

# Optional: Override defaults
ROLE_FREE=Free Member
ROLE_PREMIUM=Premium Member
CHANNEL_VERIFY=verify
# ... etc
```

**Getting Guild ID:**
- Enable Developer Mode in Discord (User Settings → Advanced)
- Right-click your server → "Copy Server ID"

**Getting SendGrid API Key:**
1. Sign up at [SendGrid](https://sendgrid.com/)
2. Go to Settings → API Keys
3. Create API Key with "Mail Send" permissions
4. Copy the key (you won't see it again!)

### 6. Run the Bot

**Option 1: Run from bot directory**
```bash
cd bot
python main.py
```

**Option 2: Run from project root**
```bash
python -m bot.main
```

The bot will:
- Initialize the database (SQLite at `data/bot.db`)
- Sync slash commands
- Start monitoring for alerts

The bot will:
- Initialize the database (SQLite at `data/bot.db`)
- Sync slash commands
- Start monitoring for alerts

## Deployment

### Railway (Recommended)

1. **Push code to GitHub** (if not already done):
   ```bash
   git add .
   git commit -m "Initial commit"
   git push origin main
   ```

2. Create account at [Railway](https://railway.app/)

3. Create new project → **"Deploy from GitHub repo"**
   - Select your repository: `curtisbw2/Hidden-Gems`
   - Railway will auto-detect Python

4. **Configure the service:**
   - Railway will auto-detect the start command from `Procfile`
   - If needed, set start command manually: `cd bot && python main.py`

5. **Add environment variables** in Railway dashboard:
   - Go to your service → Variables tab
   - Add all variables from your `.env` file:
     - `DISCORD_TOKEN`
     - `GUILD_ID`
     - `SENDGRID_API_KEY`
     - `FROM_EMAIL`
     - (and any other custom settings)

6. **Add Postgres database (Recommended for production):**
   - In Railway dashboard, click "New" → "Database" → "Add Postgres"
   - Railway will automatically create a `DATABASE_URL` environment variable
   - The bot will automatically use Postgres if `DATABASE_URL` is set
   - **Note**: If `DATABASE_URL` is not set, the bot will fall back to SQLite (for local dev)

7. **For persistent storage (SQLite fallback):**
   - If not using Postgres, Railway will persist the `data/` directory automatically
   - SQLite database will be stored in `bot/data/bot.db`

8. **Deploy:**
   - Railway will automatically deploy on every push to `main`
   - Check logs in Railway dashboard to verify bot is running

### Other Platforms

The bot can run on any platform that supports Python:
- Heroku
- DigitalOcean App Platform
- AWS EC2 / Lambda
- Google Cloud Run

**Note**: For production, use Postgres instead of SQLite for better reliability and concurrent access.

## Commands

### User Commands

- `/start` - Interactive onboarding guide
- `/verify_premium` - Request Premium via mod queue (use in #verify)
- `/submit_proof` - Submit proof attachment after `/verify_premium`
- `/link_email <email>` - Link Substack email (sends OTP)
- `/confirm_code <code> <email>` - Confirm email with OTP code
- `/status` - View bot status

### Mod/Admin Commands

- `/grant_premium <user> [reason]` - Grant Premium role
- `/revoke_premium <user> [reason]` - Revoke Premium role
- `/grant_free <user>` - Grant Free role
- `/revoke_free <user>` - Revoke Free role
- `/whois <user>` - Get user information
- `/post_access_panel` - Post or update the Access Panel in #verify (Admin only)
- `/import_paid_csv <file>` - Import paid subscribers from CSV (Admin only)
- `/sync_premium` - Sync Premium roles with paid email list (Admin only)
- `/audit_premium` - Remove Premium from users not in paid list (Admin only)

## CSV Import Format

The `/import_paid_csv` command accepts Substack export CSVs. The CSV must have an email column (case-insensitive matching for: "Email", "email", "E-mail", "email address", "subscriber email").

**Example CSV:**
```csv
Email,Name,Subscription Date
user1@example.com,John Doe,2024-01-01
user2@example.com,Jane Smith,2024-01-02
```

After import, the bot will:
1. Hash all emails (SHA-256)
2. Mark emails as active in `paid_emails` table
3. Automatically grant Premium role to users with verified emails in the list
4. Optionally revoke Premium from users not in the list (if `STRICT_REVOKE=true`)

## Configuration

All configuration is done via environment variables. See `.env.example` for all options.

**Key Settings:**
- `AUTO_ASSIGN_FREE_ON_JOIN` - Auto-assign free role on join (default: true)
- `STRICT_REVOKE` - Revoke Premium if not in paid list (default: false)
- `ALERT_TIME` - Scheduled alert time (default: 21:45 ET)
- `ALERT_THRESHOLD_PERCENT` - Alert threshold (default: 10.0%)
- `ALERT_TICKERS` - Comma-separated ticker list

## Security & Privacy

- **Email Hashing**: All emails are hashed using SHA-256 before storage
- **No Raw Images**: Proof images are not downloaded or stored; only URLs are temporarily stored
- **OTP Security**: OTP codes are hashed before storage
- **Rate Limiting**: Built-in rate limiting for verification requests
- **Permission Checks**: All admin commands check for Admin/Mod roles or Discord permissions

## Troubleshooting

**Bot doesn't assign roles:**
- Ensure bot role is above target roles in Server Settings → Roles
- Check bot has "Manage Roles" permission
- Verify SERVER MEMBERS INTENT is enabled in Developer Portal

**"PrivilegedIntentsRequired" error:**
- Go to [Discord Developer Portal](https://discord.com/developers/applications)
- Select your application → "Bot" section
- Scroll to "Privileged Gateway Intents"
- Enable **SERVER MEMBERS INTENT** and **MESSAGE CONTENT INTENT**
- Click "Save Changes"
- Restart the bot

**Commands not appearing:**
- Commands sync on startup (after bot connects)
- Use `/start` to test if commands are working
- Check bot has "applications.commands" scope in invite URL
- If you see "403 Forbidden (50001): Missing Access" error:
  - Ensure bot is invited with **both** `bot` and `applications.commands` scopes
  - Verify bot is actually in the server (check member list)
  - If using GUILD_ID, ensure it's correct (right-click server → Copy Server ID)
  - Try removing GUILD_ID from .env to use global commands instead

**Email OTP not sending:**
- Verify SendGrid API key is correct
- Check `FROM_EMAIL` is verified in SendGrid
- Check SendGrid account isn't suspended

**Alerts not triggering:**
- Check `#alerts` channel exists
- Verify ticker symbols are correct (use Yahoo Finance format)
- Check bot logs for errors

## Project Structure

```
bot/
├── main.py                 # Bot entry point
├── config.py               # Configuration management
├── db.py                   # Database abstraction (SQLite/Postgres-ready)
├── cogs/                   # Bot command modules
│   ├── onboarding.py      # Onboarding and welcome
│   ├── admin_roles.py      # Role management
│   ├── verification_queue.py  # Mod queue workflow
│   ├── email_linking.py    # Email OTP verification
│   ├── csv_import.py       # CSV import and sync
│   ├── alerts.py           # Price alerts
│   └── status.py           # Status command
├── services/               # External service integrations
│   ├── email_service.py    # SendGrid wrapper
│   ├── market_data.py      # Yahoo Finance provider
│   ├── hashing.py          # Hashing utilities
│   └── rate_limit.py       # Rate limiting
└── utils/                  # Utilities
    ├── logging.py           # Logging setup
    └── time.py              # Timezone utilities
```

## License

This bot is provided as-is for use in the Hidden Gems Research community.

## Support

For issues or questions:
1. Check the troubleshooting section
2. Review bot logs in `logs/bot.log`
3. Check `#bot-logs` channel in Discord

---
