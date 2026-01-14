"""Rate limiting utilities."""
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple
from collections import defaultdict


class RateLimiter:
    """Simple in-memory rate limiter."""
    
    def __init__(self):
        self._records: Dict[int, Dict[str, datetime]] = defaultdict(dict)
    
    def check_rate_limit(
        self, 
        user_id: int, 
        action: str, 
        cooldown_minutes: int
    ) -> Tuple[bool, Optional[datetime]]:
        """
        Check if user can perform action.
        Returns (allowed, next_allowed_time).
        """
        key = f"{action}"
        last_time = self._records[user_id].get(key)
        
        if last_time is None:
            return True, None
        
        next_allowed = last_time + timedelta(minutes=cooldown_minutes)
        now = datetime.now(timezone.utc)
        
        if now >= next_allowed:
            return True, None
        
        return False, next_allowed
    
    def record_action(self, user_id: int, action: str) -> None:
        """Record that user performed an action."""
        key = f"{action}"
        self._records[user_id][key] = datetime.now(timezone.utc)
    
    def clear_user(self, user_id: int) -> None:
        """Clear rate limit records for a user."""
        if user_id in self._records:
            del self._records[user_id]
