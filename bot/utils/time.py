"""Time utilities for scheduling."""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from typing import Optional


def get_timezone(tz_name: str) -> ZoneInfo:
    """Get timezone object."""
    try:
        return ZoneInfo(tz_name)
    except Exception:
        # Fallback to UTC if timezone not found
        return ZoneInfo("UTC")


def parse_time(time_str: str) -> time:
    """Parse time string (HH:MM) to time object."""
    try:
        hour, minute = map(int, time_str.split(":"))
        return time(hour, minute)
    except Exception:
        raise ValueError(f"Invalid time format: {time_str}. Expected HH:MM")


def get_next_run_time(target_time: time, timezone: ZoneInfo) -> datetime:
    """Get next datetime when target_time should run in given timezone."""
    now = datetime.now(timezone)
    target_dt = datetime.combine(now.date(), target_time, timezone)
    
    # If target time already passed today, schedule for tomorrow
    if target_dt <= now:
        target_dt += timedelta(days=1)
    
    return target_dt


def is_market_open(timezone: ZoneInfo = ZoneInfo("America/New_York")) -> bool:
    """Check if market is open (rough check: weekday 9:30 AM - 4:00 PM ET)."""
    now = datetime.now(timezone)
    
    # Market closed on weekends
    if now.weekday() >= 5:  # Saturday = 5, Sunday = 6
        return False
    
    # Market hours: 9:30 AM - 4:00 PM ET
    market_open = time(9, 30)
    market_close = time(16, 0)
    current_time = now.time()
    
    return market_open <= current_time <= market_close


def get_trading_day_start(timezone: ZoneInfo = ZoneInfo("America/New_York")) -> datetime:
    """Get the start of the current trading day (midnight ET)."""
    now = datetime.now(timezone)
    return datetime.combine(now.date(), time.min, timezone)
