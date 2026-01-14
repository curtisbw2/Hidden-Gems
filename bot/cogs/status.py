"""Status cog: bot status and information."""
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from utils.time import parse_time, get_next_run_time, get_timezone

logger = logging.getLogger(__name__)


class StatusCog(commands.Cog):
    """Status and information commands."""
    
    def __init__(self, bot):
        self.bot = bot
        self.config = bot.config
        self.db = bot.db
    
    @app_commands.command(name="status", description="View bot status and statistics")
    async def status(self, interaction: discord.Interaction):
        """Show bot status."""
        await interaction.response.defer(ephemeral=True)
        
        # Calculate uptime
        if self.bot.start_time:
            uptime = datetime.now(timezone.utc) - self.bot.start_time
            uptime_str = str(uptime).split(".")[0]  # Remove microseconds
        else:
            uptime_str = "Unknown"
        
        # Get last import time
        last_import = await self.db.get_last_import_time()
        if last_import:
            last_import_str = last_import.strftime("%Y-%m-%d %H:%M UTC")
        else:
            last_import_str = "Never"
        
        # Count pending verify requests
        async with self.db.get_connection() as db:
            async with db.execute(
                "SELECT COUNT(*) as count FROM verify_requests WHERE status = 'pending'"
            ) as cursor:
                row = await cursor.fetchone()
                pending_count = row["count"] if row else 0
        
        # Get next alert run time
        next_alert_str = "N/A"
        if not self.config.ALERT_CHECK_INTERVAL_MINUTES:
            try:
                tz = get_timezone(self.config.ALERT_TIMEZONE)
                target_time = parse_time(self.config.ALERT_TIME)
                next_run = get_next_run_time(target_time, tz)
                next_alert_str = next_run.strftime("%Y-%m-%d %H:%M %Z")
            except Exception:
                pass
        
        # Get last alert run time
        last_alert_run = await self.db.get_last_alert_run_time()
        if last_alert_run:
            last_alert_run_str = last_alert_run.strftime("%Y-%m-%d %H:%M UTC")
        else:
            last_alert_run_str = "Never"
        
        # Build embed
        embed = discord.Embed(
            title="💎 Bot Status",
            description="Hidden Gems Research – The Gem Vault",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.add_field(name="Uptime", value=uptime_str, inline=True)
        embed.add_field(name="Guilds", value=str(len(self.bot.guilds)), inline=True)
        embed.add_field(name="Pending Verify Requests", value=str(pending_count), inline=True)
        
        embed.add_field(name="Last CSV Import", value=last_import_str, inline=True)
        embed.add_field(name="Last Alert Run", value=last_alert_run_str, inline=True)
        embed.add_field(name="Next Alert Run", value=next_alert_str, inline=True)
        embed.add_field(name="Alert Tickers", value=", ".join(self.config.get_ticker_list()), inline=False)
        
        embed.set_footer(text=f"Bot Version: 1.0.0")
        
        await interaction.followup.send(embed=embed, ephemeral=True)
