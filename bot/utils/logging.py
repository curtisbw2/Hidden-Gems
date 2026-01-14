"""Logging configuration."""
import logging
import sys
from pathlib import Path


def setup_logging(log_level: str = "INFO"):
    """Configure logging for the bot."""
    # Create logs directory (handle both relative and absolute paths)
    try:
        Path("logs").mkdir(exist_ok=True)
        log_file = "logs/bot.log"
    except Exception:
        # If we can't create logs directory, just log to stdout
        log_file = None
    
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        try:
            handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
        except Exception:
            pass  # Fallback to stdout only
    
    # Configure root logger
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=handlers
    )
    
    # Reduce noise from discord.py
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.INFO)
