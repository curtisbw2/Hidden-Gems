"""Database management with SQLite and Postgres support."""
import os
import sqlite3
import aiosqlite
import asyncpg
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any, Union
from datetime import datetime, timezone, date as dt_date
from contextlib import asynccontextmanager
import json

logger = logging.getLogger(__name__)


class Database:
    """Database abstraction layer supporting both SQLite and Postgres."""
    
    def __init__(self, db_path: str = None, database_url: str = None):
        """
        Initialize database.
        
        Args:
            db_path: Path to SQLite database file (for local dev)
            database_url: Postgres connection string (takes precedence)
        """
        self.database_url = database_url or os.getenv("DATABASE_URL")
        self.db_path = db_path or os.getenv("DB_PATH", "data/bot.db")
        self.pool: Optional[asyncpg.Pool] = None
        self.use_postgres = bool(self.database_url)
        
        if self.use_postgres:
            logger.info("Using Postgres database")
        else:
            logger.info(f"Using SQLite database at {self.db_path}")
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
    
    async def init(self):
        """Initialize database schema and connection pool."""
        if self.use_postgres:
            await self._init_postgres()
        else:
            await self._init_sqlite()
    
    async def _init_postgres(self):
        """Initialize Postgres connection pool and schema."""
        try:
            # Create connection pool
            self.pool = await asyncpg.create_pool(
                self.database_url,
                min_size=1,
                max_size=10,
                command_timeout=60
            )
            logger.info("Postgres connection pool created")
            
            # Test connection
            async with self.pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            logger.info("Postgres connection test successful")
            
            # Create schema
            async with self.pool.acquire() as conn:
                await self._create_schema_postgres(conn)
            logger.info("Postgres schema initialized")
            
        except Exception as e:
            logger.error(f"Failed to initialize Postgres: {e}")
            raise
    
    async def _init_sqlite(self):
        """Initialize SQLite database and schema."""
        async with aiosqlite.connect(self.db_path) as db:
            await self._create_schema_sqlite(db)
            await db.commit()
        logger.info(f"SQLite database initialized at {self.db_path}")
    
    async def _create_schema_postgres(self, conn: asyncpg.Connection):
        """Create Postgres database schema."""
        # Users table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                discord_user_id BIGINT PRIMARY KEY,
                email_hash VARCHAR(64) UNIQUE,
                email_verified BOOLEAN DEFAULT FALSE,
                linked_at TIMESTAMP WITH TIME ZONE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        
        # Paid emails table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS paid_emails (
                email_hash VARCHAR(64) PRIMARY KEY,
                active BOOLEAN DEFAULT TRUE,
                last_imported_at TIMESTAMP WITH TIME ZONE,
                first_seen_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        
        # OTP codes table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS otp_codes (
                discord_user_id BIGINT,
                code_hash VARCHAR(64),
                email_hash VARCHAR(64),
                expires_at TIMESTAMP WITH TIME ZONE,
                attempts INTEGER DEFAULT 0,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                PRIMARY KEY (discord_user_id, code_hash)
            )
        """)
        
        # Verification requests table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS verify_requests (
                id BIGSERIAL PRIMARY KEY,
                discord_user_id BIGINT NOT NULL,
                claimed_email_hash VARCHAR(64),
                attachment_urls TEXT,
                status VARCHAR(20) DEFAULT 'pending',
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                reviewed_at TIMESTAMP WITH TIME ZONE,
                reviewed_by BIGINT,
                notes TEXT,
                FOREIGN KEY (discord_user_id) REFERENCES users(discord_user_id)
            )
        """)
        
        # Alert tracking table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS alert_history (
                ticker VARCHAR(10),
                alert_date DATE,
                percent_change DOUBLE PRECISION,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                PRIMARY KEY (ticker, alert_date)
            )
        """)
        
        # Alert state table (for tracking last alert date and run time per ticker)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS alert_state (
                ticker VARCHAR(10) PRIMARY KEY,
                last_alert_date DATE,
                last_alert_pct DOUBLE PRECISION,
                last_run_at TIMESTAMP WITH TIME ZONE
            )
        """)

        # Intraday state machine table (per ticker + trading_date)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS intraday_state (
                ticker VARCHAR(10) NOT NULL,
                trading_date DATE NOT NULL,
                open_price DOUBLE PRECISION,
                last_price DOUBLE PRECISION,
                last_pct DOUBLE PRECISION,
                last_zone VARCHAR(10) DEFAULT '0',
                alerted_5 BOOLEAN DEFAULT FALSE,
                alerted_10 BOOLEAN DEFAULT FALSE,
                last_alert_at TIMESTAMP WITH TIME ZONE,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                PRIMARY KEY (ticker, trading_date)
            )
        """)

        # Optional: intraday alert events log
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS intraday_alert_events (
                id BIGSERIAL PRIMARY KEY,
                ticker VARCHAR(10) NOT NULL,
                trading_date DATE NOT NULL,
                zone VARCHAR(10) NOT NULL,
                pct DOUBLE PRECISION NOT NULL,
                price DOUBLE PRECISION NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        
        # Import tracking table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS import_history (
                id BIGSERIAL PRIMARY KEY,
                imported_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                imported_by BIGINT,
                total_rows INTEGER,
                active_count INTEGER,
                granted_count INTEGER,
                revoked_count INTEGER,
                errors TEXT
            )
        """)
        
        # Access panel tracking table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS access_panel (
                guild_id BIGINT PRIMARY KEY,
                message_id BIGINT NOT NULL,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)

        # Alerts role panel tracking table (self-serve opt-in/out)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS alerts_role_panel (
                guild_id BIGINT PRIMARY KEY,
                channel_id BIGINT NOT NULL,
                message_id BIGINT NOT NULL,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        
        # Substack tracking table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS substack_posts (
                id BIGSERIAL PRIMARY KEY,
                guid VARCHAR(255) UNIQUE NOT NULL,
                posted_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        
        # Create indexes
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_email_hash ON users(email_hash)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_paid_emails_active ON paid_emails(active)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_verify_requests_status ON verify_requests(status)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_verify_requests_user ON verify_requests(discord_user_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_otp_expires ON otp_codes(expires_at)")

        # Lightweight migrations for existing databases (idempotent)
        await self._migrate_postgres(conn)

    async def _migrate_postgres(self, conn: asyncpg.Connection) -> None:
        """Best-effort Postgres migrations for evolving schemas."""
        # users
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_hash VARCHAR(64)")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS linked_at TIMESTAMP WITH TIME ZONE")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()")

        # paid_emails
        await conn.execute("ALTER TABLE paid_emails ADD COLUMN IF NOT EXISTS active BOOLEAN DEFAULT TRUE")

        # otp_codes
        await conn.execute("ALTER TABLE otp_codes ADD COLUMN IF NOT EXISTS attempts INTEGER DEFAULT 0")
        await conn.execute("ALTER TABLE otp_codes ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()")

        # intraday_state
        await conn.execute("ALTER TABLE intraday_state ADD COLUMN IF NOT EXISTS alerted_5 BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE intraday_state ADD COLUMN IF NOT EXISTS alerted_10 BOOLEAN DEFAULT FALSE")
    
    async def _create_schema_sqlite(self, db: aiosqlite.Connection):
        """Create SQLite database schema."""
        # Users table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                discord_user_id INTEGER PRIMARY KEY,
                email_hash TEXT UNIQUE,
                email_verified BOOLEAN DEFAULT FALSE,
                linked_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Paid emails table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS paid_emails (
                email_hash TEXT PRIMARY KEY,
                active BOOLEAN DEFAULT TRUE,
                last_imported_at TIMESTAMP,
                first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # OTP codes table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS otp_codes (
                discord_user_id INTEGER,
                code_hash TEXT,
                email_hash TEXT,
                expires_at TIMESTAMP,
                attempts INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (discord_user_id, code_hash)
            )
        """)
        
        # Verification requests table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS verify_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_user_id INTEGER NOT NULL,
                claimed_email_hash TEXT,
                attachment_urls TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                reviewed_at TIMESTAMP,
                reviewed_by INTEGER,
                notes TEXT,
                FOREIGN KEY (discord_user_id) REFERENCES users(discord_user_id)
            )
        """)
        
        # Alert tracking table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS alert_history (
                ticker TEXT,
                alert_date DATE,
                percent_change REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (ticker, alert_date)
            )
        """)
        
        # Alert state table (for tracking last alert date and run time per ticker)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS alert_state (
                ticker TEXT PRIMARY KEY,
                last_alert_date TEXT,
                last_alert_pct REAL,
                last_run_at TIMESTAMP
            )
        """)

        # Intraday state machine table (per ticker + trading_date)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS intraday_state (
                ticker TEXT NOT NULL,
                trading_date TEXT NOT NULL,
                open_price REAL,
                last_price REAL,
                last_pct REAL,
                last_zone TEXT DEFAULT '0',
                alerted_5 INTEGER DEFAULT 0,
                alerted_10 INTEGER DEFAULT 0,
                last_alert_at TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (ticker, trading_date)
            )
        """)

        # Optional: intraday alert events log
        await db.execute("""
            CREATE TABLE IF NOT EXISTS intraday_alert_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                trading_date TEXT NOT NULL,
                zone TEXT NOT NULL,
                pct REAL NOT NULL,
                price REAL NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Import tracking table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS import_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                imported_by INTEGER,
                total_rows INTEGER,
                active_count INTEGER,
                granted_count INTEGER,
                revoked_count INTEGER,
                errors TEXT
            )
        """)
        
        # Access panel tracking table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS access_panel (
                guild_id INTEGER PRIMARY KEY,
                message_id INTEGER NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Alerts role panel tracking table (self-serve opt-in/out)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS alerts_role_panel (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Substack tracking table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS substack_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guid TEXT UNIQUE NOT NULL,
                posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Create indexes
        await db.execute("CREATE INDEX IF NOT EXISTS idx_users_email_hash ON users(email_hash)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_paid_emails_active ON paid_emails(active)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_verify_requests_status ON verify_requests(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_verify_requests_user ON verify_requests(discord_user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_otp_expires ON otp_codes(expires_at)")

        # Lightweight migrations for existing databases (idempotent)
        await self._migrate_sqlite(db)

    async def _migrate_sqlite(self, db: aiosqlite.Connection) -> None:
        """Best-effort SQLite migrations for evolving schemas."""
        async def _columns(table: str) -> set[str]:
            async with db.execute(f"PRAGMA table_info({table})") as cursor:
                rows = await cursor.fetchall()
            return {r["name"] if isinstance(r, aiosqlite.Row) else r[1] for r in rows}

        # users
        user_cols = await _columns("users")
        if "email_hash" not in user_cols:
            await db.execute("ALTER TABLE users ADD COLUMN email_hash TEXT UNIQUE")
        if "email_verified" not in user_cols:
            await db.execute("ALTER TABLE users ADD COLUMN email_verified BOOLEAN DEFAULT FALSE")
        if "linked_at" not in user_cols:
            await db.execute("ALTER TABLE users ADD COLUMN linked_at TIMESTAMP")

        # paid_emails
        paid_cols = await _columns("paid_emails")
        if "active" not in paid_cols:
            await db.execute("ALTER TABLE paid_emails ADD COLUMN active BOOLEAN DEFAULT TRUE")

        # otp_codes
        otp_cols = await _columns("otp_codes")
        if "attempts" not in otp_cols:
            await db.execute("ALTER TABLE otp_codes ADD COLUMN attempts INTEGER DEFAULT 0")
        if "created_at" not in otp_cols:
            await db.execute("ALTER TABLE otp_codes ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")

        # intraday_state
        intraday_cols = await _columns("intraday_state")
        if "alerted_5" not in intraday_cols:
            await db.execute("ALTER TABLE intraday_state ADD COLUMN alerted_5 INTEGER DEFAULT 0")
        if "alerted_10" not in intraday_cols:
            await db.execute("ALTER TABLE intraday_state ADD COLUMN alerted_10 INTEGER DEFAULT 0")
    
    @asynccontextmanager
    async def get_connection(self):
        """Get database connection context manager."""
        if self.use_postgres:
            if not self.pool:
                raise RuntimeError("Postgres pool not initialized. Call init() first.")
            async with self.pool.acquire() as conn:
                yield conn
        else:
            db = await aiosqlite.connect(self.db_path)
            db.row_factory = aiosqlite.Row
            try:
                yield db
                await db.commit()
            finally:
                await db.close()
    
    async def close(self):
        """Close database connections."""
        if self.pool:
            await self.pool.close()
            logger.info("Postgres connection pool closed")
    
    def _row_to_dict(self, row: Union[asyncpg.Record, aiosqlite.Row]) -> Dict[str, Any]:
        """Convert database row to dictionary."""
        if self.use_postgres:
            return dict(row)
        else:
            return dict(row)

    def _pg_trading_date(self, trading_date: Union[str, dt_date]) -> dt_date:
        """Normalize trading_date for Postgres DATE columns."""
        if isinstance(trading_date, dt_date):
            return trading_date
        return dt_date.fromisoformat(str(trading_date))
    
    # User operations
    async def get_user(self, discord_user_id: int) -> Optional[Dict[str, Any]]:
        """Get user by Discord ID."""
        if self.use_postgres:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM users WHERE discord_user_id = $1",
                    discord_user_id
                )
                return dict(row) if row else None
        else:
            async with self.get_connection() as db:
                async with db.execute(
                    "SELECT * FROM users WHERE discord_user_id = ?",
                    (discord_user_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                    return dict(row) if row else None
    
    async def create_user(self, discord_user_id: int) -> None:
        """Create a new user record."""
        if self.use_postgres:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO users (discord_user_id) VALUES ($1) ON CONFLICT (discord_user_id) DO NOTHING",
                    discord_user_id
                )
        else:
            async with self.get_connection() as db:
                await db.execute(
                    "INSERT OR IGNORE INTO users (discord_user_id) VALUES (?)",
                    (discord_user_id,)
                )
    
    async def link_email(self, discord_user_id: int, email_hash: str) -> None:
        """Link email hash to user."""
        if self.use_postgres:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """UPDATE users 
                       SET email_hash = $1, email_verified = TRUE, linked_at = NOW(),
                           updated_at = NOW()
                       WHERE discord_user_id = $2""",
                    email_hash, discord_user_id
                )
        else:
            async with self.get_connection() as db:
                await db.execute(
                    """UPDATE users 
                       SET email_hash = ?, email_verified = TRUE, linked_at = CURRENT_TIMESTAMP,
                           updated_at = CURRENT_TIMESTAMP
                       WHERE discord_user_id = ?""",
                    (email_hash, discord_user_id)
                )
    
    async def get_user_by_email_hash(self, email_hash: str) -> Optional[Dict[str, Any]]:
        """Get user by email hash."""
        if self.use_postgres:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM users WHERE email_hash = $1",
                    email_hash
                )
                return dict(row) if row else None
        else:
            async with self.get_connection() as db:
                async with db.execute(
                    "SELECT * FROM users WHERE email_hash = ?",
                    (email_hash,)
                ) as cursor:
                    row = await cursor.fetchone()
                    return dict(row) if row else None
    
    # OTP operations
    async def store_otp(self, discord_user_id: int, code_hash: str, email_hash: str, expires_at: datetime) -> None:
        """Store OTP code hash with email hash."""
        # Normalize expires_at representation:
        # - Postgres can store tz-aware datetime directly
        # - SQLite comparisons are most reliable when storing ISO-8601 text
        sqlite_expires = expires_at
        if not self.use_postgres and isinstance(expires_at, datetime):
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            sqlite_expires = expires_at.isoformat()
        if self.use_postgres:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO otp_codes 
                       (discord_user_id, code_hash, email_hash, expires_at, attempts)
                       VALUES ($1, $2, $3, $4, 0)
                       ON CONFLICT (discord_user_id, code_hash) 
                       DO UPDATE SET expires_at = $4, attempts = 0""",
                    discord_user_id, code_hash, email_hash, expires_at
                )
        else:
            async with self.get_connection() as db:
                await db.execute(
                    """INSERT OR REPLACE INTO otp_codes 
                       (discord_user_id, code_hash, email_hash, expires_at, attempts)
                       VALUES (?, ?, ?, ?, 0)""",
                    (discord_user_id, code_hash, email_hash, sqlite_expires)
                )
    
    async def get_otp(self, discord_user_id: int, code_hash: str) -> Optional[Dict[str, Any]]:
        """Get OTP record."""
        now = datetime.now(timezone.utc)
        sqlite_now = now.isoformat()
        if self.use_postgres:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """SELECT * FROM otp_codes 
                       WHERE discord_user_id = $1 AND code_hash = $2 AND expires_at > $3""",
                    discord_user_id, code_hash, now
                )
                return dict(row) if row else None
        else:
            async with self.get_connection() as db:
                async with db.execute(
                    """SELECT * FROM otp_codes 
                       WHERE discord_user_id = ? AND code_hash = ? AND expires_at > ?""",
                    (discord_user_id, code_hash, sqlite_now)
                ) as cursor:
                    row = await cursor.fetchone()
                    return dict(row) if row else None
    
    async def get_otp_by_email_hash(self, discord_user_id: int, email_hash: str) -> Optional[Dict[str, Any]]:
        """Get OTP record by email hash."""
        now = datetime.now(timezone.utc)
        sqlite_now = now.isoformat()
        if self.use_postgres:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """SELECT * FROM otp_codes 
                       WHERE discord_user_id = $1 AND email_hash = $2 AND expires_at > $3
                       ORDER BY created_at DESC LIMIT 1""",
                    discord_user_id, email_hash, now
                )
                return dict(row) if row else None
        else:
            async with self.get_connection() as db:
                async with db.execute(
                    """SELECT * FROM otp_codes 
                       WHERE discord_user_id = ? AND email_hash = ? AND expires_at > ?
                       ORDER BY created_at DESC LIMIT 1""",
                    (discord_user_id, email_hash, sqlite_now)
                ) as cursor:
                    row = await cursor.fetchone()
                    return dict(row) if row else None
    
    async def increment_otp_attempts(self, discord_user_id: int, code_hash: str) -> None:
        """Increment OTP attempt count."""
        if self.use_postgres:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "UPDATE otp_codes SET attempts = attempts + 1 WHERE discord_user_id = $1 AND code_hash = $2",
                    discord_user_id, code_hash
                )
        else:
            async with self.get_connection() as db:
                await db.execute(
                    "UPDATE otp_codes SET attempts = attempts + 1 WHERE discord_user_id = ? AND code_hash = ?",
                    (discord_user_id, code_hash)
                )
    
    async def delete_otp(self, discord_user_id: int) -> None:
        """Delete OTP codes for user."""
        if self.use_postgres:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM otp_codes WHERE discord_user_id = $1",
                    discord_user_id
                )
        else:
            async with self.get_connection() as db:
                await db.execute(
                    "DELETE FROM otp_codes WHERE discord_user_id = ?",
                    (discord_user_id,)
                )

    async def delete_otps_for_user_email(self, discord_user_id: int, email_hash: str) -> None:
        """Delete OTP codes for a user scoped to a specific email hash."""
        if self.use_postgres:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM otp_codes WHERE discord_user_id = $1 AND email_hash = $2",
                    discord_user_id,
                    email_hash,
                )
        else:
            async with self.get_connection() as db:
                await db.execute(
                    "DELETE FROM otp_codes WHERE discord_user_id = ? AND email_hash = ?",
                    (discord_user_id, email_hash),
                )
    
    async def cleanup_expired_otps(self) -> None:
        """Clean up expired OTP codes."""
        now = datetime.now(timezone.utc)
        sqlite_now = now.isoformat()
        if self.use_postgres:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM otp_codes WHERE expires_at < $1",
                    now
                )
        else:
            async with self.get_connection() as db:
                await db.execute(
                    "DELETE FROM otp_codes WHERE expires_at < ?",
                    (sqlite_now,)
                )
    
    # Verification queue operations
    async def create_verify_request(
        self, 
        discord_user_id: int, 
        claimed_email_hash: Optional[str],
        attachment_urls: List[str]
    ) -> int:
        """Create a verification request. Returns request ID."""
        # Ensure user exists first (required by foreign key constraint)
        user = await self.get_user(discord_user_id)
        if not user:
            await self.create_user(discord_user_id)
        
        attachment_json = json.dumps(attachment_urls)
        if self.use_postgres:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """INSERT INTO verify_requests 
                       (discord_user_id, claimed_email_hash, attachment_urls, status)
                       VALUES ($1, $2, $3, 'pending')
                       RETURNING id""",
                    discord_user_id, claimed_email_hash, attachment_json
                )
                return row['id']
        else:
            async with self.get_connection() as db:
                cursor = await db.execute(
                    """INSERT INTO verify_requests 
                       (discord_user_id, claimed_email_hash, attachment_urls, status)
                       VALUES (?, ?, ?, 'pending')""",
                    (discord_user_id, claimed_email_hash, attachment_json)
                )
                return cursor.lastrowid
    
    async def get_pending_verify_request(self, discord_user_id: int) -> Optional[Dict[str, Any]]:
        """Get pending verification request for user."""
        if self.use_postgres:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM verify_requests WHERE discord_user_id = $1 AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
                    discord_user_id
                )
                if row:
                    result = dict(row)
                    if result.get("attachment_urls"):
                        result["attachment_urls"] = json.loads(result["attachment_urls"])
                    return result
                return None
        else:
            async with self.get_connection() as db:
                async with db.execute(
                    "SELECT * FROM verify_requests WHERE discord_user_id = ? AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
                    (discord_user_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        result = dict(row)
                        if result.get("attachment_urls"):
                            result["attachment_urls"] = json.loads(result["attachment_urls"])
                        return result
                    return None
    
    async def approve_verify_request(self, request_id: int, reviewed_by: int) -> Optional[Dict[str, Any]]:
        """Approve a verification request."""
        if self.use_postgres:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """UPDATE verify_requests 
                       SET status = 'approved', reviewed_at = NOW(), reviewed_by = $1
                       WHERE id = $2""",
                    reviewed_by, request_id
                )
                row = await conn.fetchrow(
                    "SELECT * FROM verify_requests WHERE id = $1",
                    request_id
                )
                if row:
                    result = dict(row)
                    if result.get("attachment_urls"):
                        result["attachment_urls"] = json.loads(result["attachment_urls"])
                    return result
                return None
        else:
            async with self.get_connection() as db:
                await db.execute(
                    """UPDATE verify_requests 
                       SET status = 'approved', reviewed_at = CURRENT_TIMESTAMP, reviewed_by = ?
                       WHERE id = ?""",
                    (reviewed_by, request_id)
                )
                async with db.execute(
                    "SELECT * FROM verify_requests WHERE id = ?",
                    (request_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        result = dict(row)
                        if result.get("attachment_urls"):
                            result["attachment_urls"] = json.loads(result["attachment_urls"])
                        return result
                    return None
    
    async def reject_verify_request(self, request_id: int, reviewed_by: int, notes: Optional[str] = None) -> None:
        """Reject a verification request."""
        if self.use_postgres:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """UPDATE verify_requests 
                       SET status = 'rejected', reviewed_at = NOW(), reviewed_by = $1, notes = $2
                       WHERE id = $3""",
                    reviewed_by, notes, request_id
                )
        else:
            async with self.get_connection() as db:
                await db.execute(
                    """UPDATE verify_requests 
                       SET status = 'rejected', reviewed_at = CURRENT_TIMESTAMP, reviewed_by = ?, notes = ?
                       WHERE id = ?""",
                    (reviewed_by, notes, request_id)
                )
    
    async def get_last_verify_request_time(self, discord_user_id: int) -> Optional[datetime]:
        """Get the timestamp of the last verification request (any status)."""
        if self.use_postgres:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT created_at FROM verify_requests WHERE discord_user_id = $1 ORDER BY created_at DESC LIMIT 1",
                    discord_user_id
                )
                if row:
                    return row['created_at']
                return None
        else:
            async with self.get_connection() as db:
                async with db.execute(
                    "SELECT created_at FROM verify_requests WHERE discord_user_id = ? ORDER BY created_at DESC LIMIT 1",
                    (discord_user_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        return datetime.fromisoformat(row["created_at"])
                    return None
    
    # Paid emails operations
    async def import_paid_emails(self, email_hashes: List[str]) -> Dict[str, int]:
        """Import paid email hashes. Returns counts."""
        now = datetime.now(timezone.utc)
        active_count = 0
        new_count = 0
        
        if self.use_postgres:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    for email_hash in email_hashes:
                        existing = await conn.fetchrow(
                            "SELECT active FROM paid_emails WHERE email_hash = $1",
                            email_hash
                        )
                        
                        if existing:
                            await conn.execute(
                                """UPDATE paid_emails 
                                   SET active = TRUE, last_imported_at = $1
                                   WHERE email_hash = $2""",
                                now, email_hash
                            )
                            if not existing['active']:
                                active_count += 1
                        else:
                            await conn.execute(
                                """INSERT INTO paid_emails (email_hash, active, last_imported_at)
                                   VALUES ($1, TRUE, $2)""",
                                email_hash, now
                            )
                            new_count += 1
                            active_count += 1
                    
                    # Mark emails not in this import as inactive
                    if email_hashes:
                        placeholders = ','.join(f'${i+1}' for i in range(len(email_hashes)))
                        await conn.execute(
                            f"""UPDATE paid_emails 
                               SET active = FALSE 
                               WHERE email_hash NOT IN ({placeholders})""",
                            *email_hashes
                        )
        else:
            async with self.get_connection() as db:
                for email_hash in email_hashes:
                    async with db.execute(
                        "SELECT active FROM paid_emails WHERE email_hash = ?",
                        (email_hash,)
                    ) as cursor:
                        existing = await cursor.fetchone()
                        
                        if existing:
                            await db.execute(
                                """UPDATE paid_emails 
                                   SET active = TRUE, last_imported_at = ?
                                   WHERE email_hash = ?""",
                                (now, email_hash)
                            )
                            if not existing["active"]:
                                active_count += 1
                        else:
                            await db.execute(
                                """INSERT INTO paid_emails (email_hash, active, last_imported_at)
                                   VALUES (?, TRUE, ?)""",
                                (email_hash, now)
                            )
                            new_count += 1
                            active_count += 1
                
                # Mark emails not in this import as inactive
                if email_hashes:
                    placeholders = ",".join("?" * len(email_hashes))
                    await db.execute(
                        f"""UPDATE paid_emails 
                           SET active = FALSE 
                           WHERE email_hash NOT IN ({placeholders})""",
                        email_hashes
                    )
        
        return {
            "new": new_count,
            "reactivated": active_count - new_count,
            "total_active": len(email_hashes)
        }
    
    async def is_email_paid(self, email_hash: str) -> bool:
        """Check if email hash is in paid list."""
        if self.use_postgres:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT 1 FROM paid_emails WHERE email_hash = $1 AND active = TRUE",
                    email_hash
                )
                return row is not None
        else:
            async with self.get_connection() as db:
                async with db.execute(
                    "SELECT 1 FROM paid_emails WHERE email_hash = ? AND active = TRUE",
                    (email_hash,)
                ) as cursor:
                    return (await cursor.fetchone()) is not None
    
    async def get_all_paid_emails(self) -> List[str]:
        """Get all active paid email hashes."""
        if self.use_postgres:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch("SELECT email_hash FROM paid_emails WHERE active = TRUE")
                return [row['email_hash'] for row in rows]
        else:
            async with self.get_connection() as db:
                async with db.execute(
                    "SELECT email_hash FROM paid_emails WHERE active = TRUE"
                ) as cursor:
                    rows = await cursor.fetchall()
                    return [row["email_hash"] for row in rows]
    
    # Alert operations
    async def has_alerted_today(self, ticker: str) -> bool:
        """Check if we've already alerted for this ticker today."""
        today = datetime.now(timezone.utc).date()
        if self.use_postgres:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT 1 FROM alert_history WHERE ticker = $1 AND alert_date = $2",
                    ticker, today
                )
                return row is not None
        else:
            async with self.get_connection() as db:
                async with db.execute(
                    "SELECT 1 FROM alert_history WHERE ticker = ? AND alert_date = ?",
                    (ticker, today)
                ) as cursor:
                    return (await cursor.fetchone()) is not None
    
    async def record_alert(self, ticker: str, percent_change: float) -> None:
        """Record that we've sent an alert for this ticker today."""
        today = datetime.now(timezone.utc).date()
        if self.use_postgres:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO alert_history (ticker, alert_date, percent_change)
                       VALUES ($1, $2, $3)
                       ON CONFLICT (ticker, alert_date) 
                       DO UPDATE SET percent_change = $3""",
                    ticker, today, percent_change
                )
        else:
            async with self.get_connection() as db:
                await db.execute(
                    """INSERT OR REPLACE INTO alert_history (ticker, alert_date, percent_change)
                       VALUES (?, ?, ?)""",
                    (ticker, today, percent_change)
                )
    
    # Alert state operations
    async def get_alert_state(self, ticker: str) -> Optional[Dict[str, Any]]:
        """Get alert state for ticker."""
        if self.use_postgres:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM alert_state WHERE ticker = $1",
                    ticker
                )
                return dict(row) if row else None
        else:
            async with self.get_connection() as db:
                async with db.execute(
                    "SELECT * FROM alert_state WHERE ticker = ?",
                    (ticker,)
                ) as cursor:
                    row = await cursor.fetchone()
                    return dict(row) if row else None
    
    async def update_alert_state(
        self, 
        ticker: str, 
        last_alert_date: Optional[str], 
        last_alert_pct: Optional[float],
        last_run_at: Optional[datetime] = None
    ) -> None:
        """Update alert state for ticker."""
        if last_run_at is None:
            last_run_at = datetime.now(timezone.utc)
        
        if self.use_postgres:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO alert_state (ticker, last_alert_date, last_alert_pct, last_run_at)
                       VALUES ($1, $2, $3, $4)
                       ON CONFLICT (ticker) 
                       DO UPDATE SET 
                           last_alert_date = COALESCE($2, alert_state.last_alert_date),
                           last_alert_pct = COALESCE($3, alert_state.last_alert_pct),
                           last_run_at = $4""",
                    ticker, last_alert_date, last_alert_pct, last_run_at
                )
        else:
            async with self.get_connection() as db:
                await db.execute(
                    """INSERT OR REPLACE INTO alert_state (ticker, last_alert_date, last_alert_pct, last_run_at)
                       VALUES (?, ?, ?, ?)""",
                    (ticker, last_alert_date, last_alert_pct, last_run_at)
                )
    
    async def get_last_alert_run_time(self) -> Optional[datetime]:
        """Get the most recent last_run_at timestamp across all tickers."""
        if self.use_postgres:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT MAX(last_run_at) as max_run_at FROM alert_state"
                )
                return row['max_run_at'] if row and row['max_run_at'] else None
        else:
            async with self.get_connection() as db:
                async with db.execute(
                    "SELECT MAX(last_run_at) as max_run_at FROM alert_state"
                ) as cursor:
                    row = await cursor.fetchone()
                    if row and row["max_run_at"]:
                        return datetime.fromisoformat(row["max_run_at"])
                    return None

    # Intraday alert state operations
    async def get_intraday_state(self, ticker: str, trading_date: str) -> Optional[Dict[str, Any]]:
        """Get intraday state for ticker + trading_date (YYYY-MM-DD ET)."""
        if self.use_postgres:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM intraday_state WHERE ticker = $1 AND trading_date = $2",
                    ticker,
                    self._pg_trading_date(trading_date),
                )
                return dict(row) if row else None
        else:
            async with self.get_connection() as db:
                async with db.execute(
                    "SELECT * FROM intraday_state WHERE ticker = ? AND trading_date = ?",
                    (ticker, trading_date),
                ) as cursor:
                    row = await cursor.fetchone()
                    return dict(row) if row else None

    async def upsert_intraday_state(
        self,
        ticker: str,
        trading_date: str,
        open_price: Optional[float],
        last_price: Optional[float],
        last_pct: Optional[float],
        last_zone: str,
        alerted_5: bool = False,
        alerted_10: bool = False,
        last_alert_at: Optional[datetime] = None,
        updated_at: Optional[datetime] = None,
    ) -> None:
        """Insert/update intraday state row."""
        if updated_at is None:
            updated_at = datetime.now(timezone.utc)

        sqlite_last_alert_at = last_alert_at
        sqlite_updated_at = updated_at
        if not self.use_postgres:
            if isinstance(last_alert_at, datetime):
                if last_alert_at.tzinfo is None:
                    last_alert_at = last_alert_at.replace(tzinfo=timezone.utc)
                sqlite_last_alert_at = last_alert_at.isoformat()
            if isinstance(updated_at, datetime):
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=timezone.utc)
                sqlite_updated_at = updated_at.isoformat()

        if self.use_postgres:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO intraday_state
                        (ticker, trading_date, open_price, last_price, last_pct, last_zone, alerted_5, alerted_10, last_alert_at, updated_at)
                    VALUES
                        ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    ON CONFLICT (ticker, trading_date)
                    DO UPDATE SET
                        open_price = COALESCE($3, intraday_state.open_price),
                        last_price = $4,
                        last_pct = $5,
                        last_zone = $6,
                        alerted_5 = $7,
                        alerted_10 = $8,
                        last_alert_at = $9,
                        updated_at = $10
                    """,
                    ticker,
                    self._pg_trading_date(trading_date),
                    open_price,
                    last_price,
                    last_pct,
                    last_zone,
                    bool(alerted_5),
                    bool(alerted_10),
                    last_alert_at,
                    updated_at,
                )
        else:
            async with self.get_connection() as db:
                await db.execute(
                    """
                    INSERT OR REPLACE INTO intraday_state
                        (ticker, trading_date, open_price, last_price, last_pct, last_zone, alerted_5, alerted_10, last_alert_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ticker,
                        trading_date,
                        open_price,
                        last_price,
                        last_pct,
                        last_zone,
                        1 if alerted_5 else 0,
                        1 if alerted_10 else 0,
                        sqlite_last_alert_at,
                        sqlite_updated_at,
                    ),
                )

    async def delete_intraday_state(self, trading_date: str, ticker: Optional[str] = None) -> int:
        """Delete intraday state rows for a trading_date; optionally for a single ticker. Returns rows deleted."""
        if self.use_postgres:
            async with self.pool.acquire() as conn:
                if ticker:
                    res = await conn.execute(
                        "DELETE FROM intraday_state WHERE trading_date = $1 AND ticker = $2",
                        self._pg_trading_date(trading_date),
                        ticker,
                    )
                else:
                    res = await conn.execute(
                        "DELETE FROM intraday_state WHERE trading_date = $1",
                        self._pg_trading_date(trading_date),
                    )
                # asyncpg returns like "DELETE <n>"
                try:
                    return int(str(res).split()[-1])
                except Exception:
                    return 0
        else:
            async with self.get_connection() as db:
                if ticker:
                    cursor = await db.execute(
                        "DELETE FROM intraday_state WHERE trading_date = ? AND ticker = ?",
                        (trading_date, ticker),
                    )
                else:
                    cursor = await db.execute(
                        "DELETE FROM intraday_state WHERE trading_date = ?",
                        (trading_date,),
                    )
                return cursor.rowcount or 0

    async def record_intraday_alert_event(
        self,
        ticker: str,
        trading_date: str,
        zone: str,
        pct: float,
        price: float,
        created_at: Optional[datetime] = None,
    ) -> None:
        """Record an intraday alert event (optional audit log)."""
        if created_at is None:
            created_at = datetime.now(timezone.utc)

        sqlite_created_at = created_at
        if not self.use_postgres:
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            sqlite_created_at = created_at.isoformat()

        if self.use_postgres:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO intraday_alert_events (ticker, trading_date, zone, pct, price, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    ticker,
                    self._pg_trading_date(trading_date),
                    zone,
                    pct,
                    price,
                    created_at,
                )
        else:
            async with self.get_connection() as db:
                await db.execute(
                    """
                    INSERT INTO intraday_alert_events (ticker, trading_date, zone, pct, price, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (ticker, trading_date, zone, pct, price, sqlite_created_at),
                )

    async def claim_intraday_zone_entry(
        self,
        *,
        ticker: str,
        trading_date: str,
        prev_zone: str,
        new_zone: str,
        baseline_price: Optional[float] = None,
        last_price: Optional[float] = None,
        last_pct: Optional[float] = None,
        now_utc: Optional[datetime] = None,
    ) -> bool:
        """
        Atomically "claim" a zone entry so we only alert once per actual transition.

        Returns True if this call successfully claimed the transition (i.e., should send alert),
        False if another process/loop already claimed it or if no change happened.
        """
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)

        prev_zone = (prev_zone or "0").strip()
        new_zone = (new_zone or "0").strip()

        if self.use_postgres:
            async with self.pool.acquire() as conn:
                # 1) Try insert (first time today) — only one will win
                inserted = await conn.fetchrow(
                    """
                    INSERT INTO intraday_state
                        (ticker, trading_date, open_price, last_price, last_pct, last_zone, last_alert_at, updated_at)
                    VALUES
                        ($1, $2, $3, $4, $5, $6, $7, $7)
                    ON CONFLICT (ticker, trading_date) DO NOTHING
                    RETURNING 1
                    """,
                    ticker,
                    self._pg_trading_date(trading_date),
                    baseline_price,
                    last_price,
                    last_pct,
                    new_zone,
                    now_utc,
                )
                if inserted:
                    return True

                # 2) Compare-and-set update: only claim if DB still has prev_zone
                updated = await conn.fetchrow(
                    """
                    UPDATE intraday_state
                    SET last_zone = $1, last_alert_at = $2, updated_at = $2
                    WHERE ticker = $3 AND trading_date = $4 AND last_zone = $5
                    RETURNING 1
                    """,
                    new_zone,
                    now_utc,
                    ticker,
                    self._pg_trading_date(trading_date),
                    prev_zone,
                )
                return bool(updated)
        else:
            async with self.get_connection() as db:
                # 1) Try insert (first time today) — only one will win
                cursor = await db.execute(
                    """
                    INSERT OR IGNORE INTO intraday_state
                        (ticker, trading_date, open_price, last_price, last_pct, last_zone, last_alert_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ticker,
                        trading_date,
                        baseline_price,
                        last_price,
                        last_pct,
                        new_zone,
                        now_utc.isoformat() if isinstance(now_utc, datetime) else now_utc,
                        now_utc.isoformat() if isinstance(now_utc, datetime) else now_utc,
                    ),
                )
                if cursor.rowcount and cursor.rowcount > 0:
                    return True

                # 2) Compare-and-set update: only claim if DB still has prev_zone
                cursor2 = await db.execute(
                    """
                    UPDATE intraday_state
                    SET last_zone = ?, last_alert_at = ?, updated_at = ?
                    WHERE ticker = ? AND trading_date = ? AND last_zone = ?
                    """,
                    (
                        new_zone,
                        now_utc.isoformat(),
                        now_utc.isoformat(),
                        ticker,
                        trading_date,
                        prev_zone,
                    ),
                )
                return bool(cursor2.rowcount and cursor2.rowcount > 0)
    
    # Import history
    async def record_import(
        self,
        imported_by: int,
        total_rows: int,
        active_count: int,
        granted_count: int,
        revoked_count: int,
        errors: List[str]
    ) -> int:
        """Record CSV import. Returns import ID."""
        errors_json = json.dumps(errors)
        if self.use_postgres:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """INSERT INTO import_history 
                       (imported_by, total_rows, active_count, granted_count, revoked_count, errors)
                       VALUES ($1, $2, $3, $4, $5, $6)
                       RETURNING id""",
                    imported_by, total_rows, active_count, granted_count, revoked_count, errors_json
                )
                return row['id']
        else:
            async with self.get_connection() as db:
                cursor = await db.execute(
                    """INSERT INTO import_history 
                       (imported_by, total_rows, active_count, granted_count, revoked_count, errors)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (imported_by, total_rows, active_count, granted_count, revoked_count, errors_json)
                )
                return cursor.lastrowid
    
    async def get_last_import_time(self) -> Optional[datetime]:
        """Get timestamp of last CSV import."""
        if self.use_postgres:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT imported_at FROM import_history ORDER BY imported_at DESC LIMIT 1"
                )
                if row:
                    return row['imported_at']
                return None
        else:
            async with self.get_connection() as db:
                async with db.execute(
                    "SELECT imported_at FROM import_history ORDER BY imported_at DESC LIMIT 1"
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        return datetime.fromisoformat(row["imported_at"])
                    return None
    
    # Access panel operations
    async def get_access_panel_message_id(self, guild_id: int) -> Optional[int]:
        """Get access panel message ID for guild."""
        if self.use_postgres:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT message_id FROM access_panel WHERE guild_id = $1",
                    guild_id
                )
                return row['message_id'] if row else None
        else:
            async with self.get_connection() as db:
                async with db.execute(
                    "SELECT message_id FROM access_panel WHERE guild_id = ?",
                    (guild_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                    return row["message_id"] if row else None
    
    async def set_access_panel_message_id(self, guild_id: int, message_id: int) -> None:
        """Set access panel message ID for guild."""
        if self.use_postgres:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO access_panel (guild_id, message_id, updated_at)
                       VALUES ($1, $2, NOW())
                       ON CONFLICT (guild_id) 
                       DO UPDATE SET message_id = $2, updated_at = NOW()""",
                    guild_id, message_id
                )
        else:
            async with self.get_connection() as db:
                await db.execute(
                    """INSERT OR REPLACE INTO access_panel (guild_id, message_id, updated_at)
                       VALUES (?, ?, CURRENT_TIMESTAMP)""",
                    (guild_id, message_id)
                )
    
    async def update_access_panel_message_id(self, guild_id: int, message_id: int) -> None:
        """Update access panel message ID for guild."""
        await self.set_access_panel_message_id(guild_id, message_id)

    # Alerts role panel operations
    async def get_alerts_role_panel(self, guild_id: int) -> Optional[Dict[str, Any]]:
        """Get alerts role panel info for guild."""
        if self.use_postgres:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT guild_id, channel_id, message_id FROM alerts_role_panel WHERE guild_id = $1",
                    guild_id,
                )
                return dict(row) if row else None
        else:
            async with self.get_connection() as db:
                async with db.execute(
                    "SELECT guild_id, channel_id, message_id FROM alerts_role_panel WHERE guild_id = ?",
                    (guild_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                    return dict(row) if row else None

    async def set_alerts_role_panel(self, guild_id: int, channel_id: int, message_id: int) -> None:
        """Set alerts role panel message/channel for guild."""
        if self.use_postgres:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO alerts_role_panel (guild_id, channel_id, message_id, updated_at)
                    VALUES ($1, $2, $3, NOW())
                    ON CONFLICT (guild_id)
                    DO UPDATE SET channel_id = $2, message_id = $3, updated_at = NOW()
                    """,
                    guild_id,
                    channel_id,
                    message_id,
                )
        else:
            async with self.get_connection() as db:
                await db.execute(
                    """
                    INSERT OR REPLACE INTO alerts_role_panel (guild_id, channel_id, message_id, updated_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (guild_id, channel_id, message_id),
                )
