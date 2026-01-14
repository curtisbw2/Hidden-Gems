"""Logging configuration."""
import logging
import sys
from pathlib import Path


def setup_logging(log_level: str = "INFO"):
    """Configure logging for the bot."""
    # Create logs directory
    Path("logs").mkdir(exist_ok=True)
    
    # Configure root logger
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler("logs/bot.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout)
        ]
    )
    
    # Reduce noise from discord.py
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.INFO)
