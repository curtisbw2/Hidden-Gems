"""Configuration management for Hidden Gems bot."""
import os
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

# Try to load .env file if python-dotenv is available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv not installed, use system environment variables


@dataclass
class Config:
    """Bot configuration loaded from environment variables."""
    
    # Discord
    DISCORD_TOKEN: str
    GUILD_ID: Optional[int] = None
    BOT_PREFIX: str = "!"
    
    # Roles (default names, can be overridden with IDs)
    ROLE_FREE: str = "Free Member"
    ROLE_PREMIUM: str = "Premium Member"
    ROLE_ADMIN: str = "Admin"
    ROLE_MOD: str = "Mod"
    ROLE_FREE_ID: Optional[int] = None
    ROLE_PREMIUM_ID: Optional[int] = None
    ROLE_ADMIN_ID: Optional[int] = None
    ROLE_MOD_ID: Optional[int] = None
    
    # Channels (default names, can be overridden with IDs)
    CHANNEL_VERIFY: str = "verify"
    CHANNEL_VERIFY_QUEUE: str = "verify-queue"
    CHANNEL_BOT_LOGS: str = "bot-logs"
    CHANNEL_ALERTS: str = "alerts"
    CHANNEL_VERIFY_ID: Optional[int] = None
    CHANNEL_VERIFY_QUEUE_ID: Optional[int] = None
    CHANNEL_BOT_LOGS_ID: Optional[int] = None
    CHANNEL_ALERTS_ID: Optional[int] = None
    CHANNEL_FALLBACK_DM: Optional[int] = None  # Optional fallback for failed DMs
    
    # Onboarding
    AUTO_ASSIGN_FREE_ON_JOIN: bool = True
    
    # Email/OTP
    SENDGRID_API_KEY: Optional[str] = None
    FROM_EMAIL: Optional[str] = None
    OTP_EXPIRY_MINUTES: int = 10
    OTP_MAX_ATTEMPTS: int = 5
    
    # Verification Queue
    VERIFY_COOLDOWN_MINUTES: int = 30
    PROOF_TIMEOUT_MINUTES: int = 10
    
    # CSV Import
    STRICT_REVOKE: bool = False  # If True, revoke Premium if not in paid list
    
    # Alerts
    ALERT_TIME: str = "21:45"  # HH:MM format
    ALERT_TIMEZONE: str = "America/New_York"
    ALERT_CHECK_INTERVAL_MINUTES: Optional[int] = None  # If set, check every N minutes but only alert once per day
    ALERT_THRESHOLD_PERCENT: float = 10.0
    ALERT_TICKERS: str = "RR,ONDS,ACHR,UMAC,AMPX,LPTH"
    
    # Database
    DB_PATH: str = "data/bot.db"
    
    # Logging
    LOG_LEVEL: str = "INFO"
    
    @classmethod
    def from_env(cls) -> "Config":
        """Load configuration from environment variables."""
        def get_bool(key: str, default: bool = False) -> bool:
            val = os.getenv(key)
            if val is None:
                return default
            return val.lower() in ("true", "1", "yes", "on")
        
        def get_int(key: str, default: Optional[int] = None) -> Optional[int]:
            val = os.getenv(key)
            if val is None:
                return default
            try:
                return int(val)
            except ValueError:
                return default
        
        def get_float(key: str, default: float = 0.0) -> float:
            val = os.getenv(key)
            if val is None:
                return default
            try:
                return float(val)
            except ValueError:
                return default
        
        guild_id = get_int("GUILD_ID")
        
        return cls(
            DISCORD_TOKEN=os.getenv("DISCORD_TOKEN", ""),
            GUILD_ID=guild_id,
            BOT_PREFIX=os.getenv("BOT_PREFIX", "!"),
            
            ROLE_FREE=os.getenv("ROLE_FREE", "Free Member"),
            ROLE_PREMIUM=os.getenv("ROLE_PREMIUM", "Premium Member"),
            ROLE_ADMIN=os.getenv("ROLE_ADMIN", "Admin"),
            ROLE_MOD=os.getenv("ROLE_MOD", "Mod"),
            ROLE_FREE_ID=get_int("ROLE_FREE_ID"),
            ROLE_PREMIUM_ID=get_int("ROLE_PREMIUM_ID"),
            ROLE_ADMIN_ID=get_int("ROLE_ADMIN_ID"),
            ROLE_MOD_ID=get_int("ROLE_MOD_ID"),
            
            CHANNEL_VERIFY=os.getenv("CHANNEL_VERIFY", "verify"),
            CHANNEL_VERIFY_QUEUE=os.getenv("CHANNEL_VERIFY_QUEUE", "verify-queue"),
            CHANNEL_BOT_LOGS=os.getenv("CHANNEL_BOT_LOGS", "bot-logs"),
            CHANNEL_ALERTS=os.getenv("CHANNEL_ALERTS", "alerts"),
            CHANNEL_VERIFY_ID=get_int("CHANNEL_VERIFY_ID"),
            CHANNEL_VERIFY_QUEUE_ID=get_int("CHANNEL_VERIFY_QUEUE_ID"),
            CHANNEL_BOT_LOGS_ID=get_int("CHANNEL_BOT_LOGS_ID"),
            CHANNEL_ALERTS_ID=get_int("CHANNEL_ALERTS_ID"),
            CHANNEL_FALLBACK_DM=get_int("CHANNEL_FALLBACK_DM"),
            
            AUTO_ASSIGN_FREE_ON_JOIN=get_bool("AUTO_ASSIGN_FREE_ON_JOIN", True),
            
            SENDGRID_API_KEY=os.getenv("SENDGRID_API_KEY"),
            FROM_EMAIL=os.getenv("FROM_EMAIL"),
            OTP_EXPIRY_MINUTES=get_int("OTP_EXPIRY_MINUTES", 10),
            OTP_MAX_ATTEMPTS=get_int("OTP_MAX_ATTEMPTS", 5),
            
            VERIFY_COOLDOWN_MINUTES=get_int("VERIFY_COOLDOWN_MINUTES", 30),
            PROOF_TIMEOUT_MINUTES=get_int("PROOF_TIMEOUT_MINUTES", 10),
            
            STRICT_REVOKE=get_bool("STRICT_REVOKE", False),
            
            ALERT_TIME=os.getenv("ALERT_TIME", "21:45"),
            ALERT_TIMEZONE=os.getenv("ALERT_TIMEZONE", "America/New_York"),
            ALERT_CHECK_INTERVAL_MINUTES=get_int("ALERT_CHECK_INTERVAL_MINUTES"),
            ALERT_THRESHOLD_PERCENT=get_float("ALERT_THRESHOLD_PERCENT", 10.0),
            ALERT_TICKERS=os.getenv("ALERT_TICKERS", "RR,ONDS,ACHR,UMAC,AMPX,LPTH"),
            
            DB_PATH=os.getenv("DB_PATH", "data/bot.db"),
            LOG_LEVEL=os.getenv("LOG_LEVEL", "INFO"),
        )
    
    def get_role_ids(self) -> dict[str, Optional[int]]:
        """Get role IDs, preferring explicit IDs over names."""
        return {
            "free": self.ROLE_FREE_ID,
            "premium": self.ROLE_PREMIUM_ID,
            "admin": self.ROLE_ADMIN_ID,
            "mod": self.ROLE_MOD_ID,
        }
    
    def get_channel_ids(self) -> dict[str, Optional[int]]:
        """Get channel IDs, preferring explicit IDs over names."""
        return {
            "verify": self.CHANNEL_VERIFY_ID,
            "verify_queue": self.CHANNEL_VERIFY_QUEUE_ID,
            "bot_logs": self.CHANNEL_BOT_LOGS_ID,
            "alerts": self.CHANNEL_ALERTS_ID,
        }
    
    def get_ticker_list(self) -> list[str]:
        """Parse ticker list from config."""
        return [t.strip().upper() for t in self.ALERT_TICKERS.split(",") if t.strip()]
