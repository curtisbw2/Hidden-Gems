"""Database management with SQLite (Postgres-ready abstraction)."""
import sqlite3
import aiosqlite
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)


class Database:
    """Database abstraction layer for SQLite (can be swapped for Postgres)."""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    
    async def init(self):
        """Initialize database schema."""
        async with aiosqlite.connect(self.db_path) as db:
            await self._create_schema(db)
            await db.commit()
        logger.info(f"Database initialized at {self.db_path}")
    
    async def _create_schema(self, db: aiosqlite.Connection):
        """Create database schema."""
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
        
        # Paid emails table (from CSV imports)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS paid_emails (
                email_hash TEXT PRIMARY KEY,
                active BOOLEAN DEFAULT TRUE,
                last_imported_at TIMESTAMP,
                first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # OTP codes table (temporary)
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
                attachment_urls TEXT,  -- JSON array of URLs
                status TEXT DEFAULT 'pending',  -- pending, approved, rejected
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
                errors TEXT  -- JSON array of errors
            )
        """)
        
        # Create indexes
        await db.execute("CREATE INDEX IF NOT EXISTS idx_users_email_hash ON users(email_hash)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_paid_emails_active ON paid_emails(active)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_verify_requests_status ON verify_requests(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_verify_requests_user ON verify_requests(discord_user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_otp_expires ON otp_codes(expires_at)")
    
    @asynccontextmanager
    async def get_connection(self):
        """Get database connection context manager."""
        db = await aiosqlite.connect(self.db_path)
        db.row_factory = aiosqlite.Row
        try:
            yield db
            await db.commit()
        finally:
            await db.close()
    
    # User operations
    async def get_user(self, discord_user_id: int) -> Optional[Dict[str, Any]]:
        """Get user by Discord ID."""
        async with self.get_connection() as db:
            async with db.execute(
                "SELECT * FROM users WHERE discord_user_id = ?",
                (discord_user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None
    
    async def create_user(self, discord_user_id: int) -> None:
        """Create a new user record."""
        async with self.get_connection() as db:
            await db.execute(
                "INSERT OR IGNORE INTO users (discord_user_id) VALUES (?)",
                (discord_user_id,)
            )
    
    async def link_email(self, discord_user_id: int, email_hash: str) -> None:
        """Link email hash to user."""
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
        async with self.get_connection() as db:
            await db.execute(
                """INSERT OR REPLACE INTO otp_codes 
                   (discord_user_id, code_hash, email_hash, expires_at, attempts)
                   VALUES (?, ?, ?, ?, 0)""",
                (discord_user_id, code_hash, email_hash, expires_at)
            )
    
    async def get_otp(self, discord_user_id: int, code_hash: str) -> Optional[Dict[str, Any]]:
        """Get OTP record."""
        async with self.get_connection() as db:
            async with db.execute(
                """SELECT * FROM otp_codes 
                   WHERE discord_user_id = ? AND code_hash = ? AND expires_at > ?""",
                (discord_user_id, code_hash, datetime.now(timezone.utc))
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None
    
    async def get_otp_by_email_hash(self, discord_user_id: int, email_hash: str) -> Optional[Dict[str, Any]]:
        """Get OTP record by email hash."""
        async with self.get_connection() as db:
            async with db.execute(
                """SELECT * FROM otp_codes 
                   WHERE discord_user_id = ? AND email_hash = ? AND expires_at > ?
                   ORDER BY created_at DESC LIMIT 1""",
                (discord_user_id, email_hash, datetime.now(timezone.utc))
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None
    
    async def increment_otp_attempts(self, discord_user_id: int, code_hash: str) -> None:
        """Increment OTP attempt count."""
        async with self.get_connection() as db:
            await db.execute(
                "UPDATE otp_codes SET attempts = attempts + 1 WHERE discord_user_id = ? AND code_hash = ?",
                (discord_user_id, code_hash)
            )
    
    async def delete_otp(self, discord_user_id: int) -> None:
        """Delete OTP codes for user."""
        async with self.get_connection() as db:
            await db.execute(
                "DELETE FROM otp_codes WHERE discord_user_id = ?",
                (discord_user_id,)
            )
    
    async def cleanup_expired_otps(self) -> None:
        """Clean up expired OTP codes."""
        async with self.get_connection() as db:
            await db.execute(
                "DELETE FROM otp_codes WHERE expires_at < ?",
                (datetime.now(timezone.utc),)
            )
    
    # Verification queue operations
    async def create_verify_request(
        self, 
        discord_user_id: int, 
        claimed_email_hash: Optional[str],
        attachment_urls: List[str]
    ) -> int:
        """Create a verification request. Returns request ID."""
        async with self.get_connection() as db:
            import json
            cursor = await db.execute(
                """INSERT INTO verify_requests 
                   (discord_user_id, claimed_email_hash, attachment_urls, status)
                   VALUES (?, ?, ?, 'pending')""",
                (discord_user_id, claimed_email_hash, json.dumps(attachment_urls))
            )
            return cursor.lastrowid
    
    async def get_pending_verify_request(self, discord_user_id: int) -> Optional[Dict[str, Any]]:
        """Get pending verification request for user."""
        async with self.get_connection() as db:
            async with db.execute(
                "SELECT * FROM verify_requests WHERE discord_user_id = ? AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
                (discord_user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    import json
                    result = dict(row)
                    if result.get("attachment_urls"):
                        result["attachment_urls"] = json.loads(result["attachment_urls"])
                    return result
                return None
    
    async def approve_verify_request(self, request_id: int, reviewed_by: int) -> Optional[Dict[str, Any]]:
        """Approve a verification request."""
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
                    import json
                    result = dict(row)
                    if result.get("attachment_urls"):
                        result["attachment_urls"] = json.loads(result["attachment_urls"])
                    return result
                return None
    
    async def reject_verify_request(self, request_id: int, reviewed_by: int, notes: Optional[str] = None) -> None:
        """Reject a verification request."""
        async with self.get_connection() as db:
            await db.execute(
                """UPDATE verify_requests 
                   SET status = 'rejected', reviewed_at = CURRENT_TIMESTAMP, reviewed_by = ?, notes = ?
                   WHERE id = ?""",
                (reviewed_by, notes, request_id)
            )
    
    async def get_last_verify_request_time(self, discord_user_id: int) -> Optional[datetime]:
        """Get the timestamp of the last verification request (any status)."""
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
        async with self.get_connection() as db:
            now = datetime.now(timezone.utc)
            active_count = 0
            new_count = 0
            
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
        async with self.get_connection() as db:
            async with db.execute(
                "SELECT 1 FROM paid_emails WHERE email_hash = ? AND active = TRUE",
                (email_hash,)
            ) as cursor:
                return (await cursor.fetchone()) is not None
    
    async def get_all_paid_emails(self) -> List[str]:
        """Get all active paid email hashes."""
        async with self.get_connection() as db:
            async with db.execute(
                "SELECT email_hash FROM paid_emails WHERE active = TRUE"
            ) as cursor:
                rows = await cursor.fetchall()
                return [row["email_hash"] for row in rows]
    
    # Alert operations
    async def has_alerted_today(self, ticker: str) -> bool:
        """Check if we've already alerted for this ticker today."""
        async with self.get_connection() as db:
            today = datetime.now(timezone.utc).date()
            async with db.execute(
                "SELECT 1 FROM alert_history WHERE ticker = ? AND alert_date = ?",
                (ticker, today)
            ) as cursor:
                return (await cursor.fetchone()) is not None
    
    async def record_alert(self, ticker: str, percent_change: float) -> None:
        """Record that we've sent an alert for this ticker today."""
        async with self.get_connection() as db:
            today = datetime.now(timezone.utc).date()
            await db.execute(
                """INSERT OR REPLACE INTO alert_history (ticker, alert_date, percent_change)
                   VALUES (?, ?, ?)""",
                (ticker, today, percent_change)
            )
    
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
        async with self.get_connection() as db:
            import json
            cursor = await db.execute(
                """INSERT INTO import_history 
                   (imported_by, total_rows, active_count, granted_count, revoked_count, errors)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (imported_by, total_rows, active_count, granted_count, revoked_count, json.dumps(errors))
            )
            return cursor.lastrowid
    
    async def get_last_import_time(self) -> Optional[datetime]:
        """Get timestamp of last CSV import."""
        async with self.get_connection() as db:
            async with db.execute(
                "SELECT imported_at FROM import_history ORDER BY imported_at DESC LIMIT 1"
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return datetime.fromisoformat(row["imported_at"])
                return None
