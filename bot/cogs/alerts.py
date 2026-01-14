"""Price alerts cog: daily price change monitoring."""
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

from services.market_data import YahooFinanceProvider
from utils.time import parse_time, get_next_run_time, get_timezone, is_market_open, get_trading_day_start

logger = logging.getLogger(__name__)


class AlertsCog(commands.Cog):
    """Price alerts functionality."""
    
    def __init__(self, bot):
        self.bot = bot
        self.config = bot.config
        self.db = bot.db
        self.market_data = YahooFinanceProvider()
        self.tickers = self.config.get_ticker_list()
        self.last_alert_date = None
        
        # Start alert task
        self.alert_task.start()
    
    def cog_unload(self):
        """Cleanup when cog is unloaded."""
        self.alert_task.cancel()
    
    async def get_channel(self, guild: discord.Guild, channel_type: str) -> discord.TextChannel | None:
        """Get channel by type or ID."""
        channel_ids = self.config.get_channel_ids()
        channel_names = {
            "verify": self.config.CHANNEL_VERIFY,
            "verify_queue": self.config.CHANNEL_VERIFY_QUEUE,
            "bot_logs": self.config.CHANNEL_BOT_LOGS,
            "alerts": self.config.CHANNEL_ALERTS,
        }
        
        channel_id = channel_ids.get(channel_type)
        if channel_id:
            channel = guild.get_channel(channel_id)
            if channel and isinstance(channel, discord.TextChannel):
                return channel
        
        channel_name = channel_names.get(channel_type)
        if channel_name:
            channel = discord.utils.get(guild.text_channels, name=channel_name)
            if channel:
                return channel
        
        return None
    
    @tasks.loop(minutes=1.0)
    async def alert_task(self):
        """Check for price alerts."""
        try:
            # If interval is set, check every N minutes but only alert once per day
            if self.config.ALERT_CHECK_INTERVAL_MINUTES:
                # Check if we've already alerted today
                today = datetime.now(timezone.utc).date()
                if self.last_alert_date == today:
                    return
                
                # Check if market is open (rough check)
                tz = get_timezone(self.config.ALERT_TIMEZONE)
                if not is_market_open(tz):
                    return
                
                # Run checks
                await self.check_alerts()
                self.last_alert_date = today
            else:
                # Scheduled time approach
                tz = get_timezone(self.config.ALERT_TIMEZONE)
                target_time = parse_time(self.config.ALERT_TIME)
                next_run = get_next_run_time(target_time, tz)
                now = datetime.now(tz)
                
                # Check if it's time to run (within 1 minute window)
                if abs((now - next_run).total_seconds()) < 60:
                    await self.check_alerts()
        except Exception as e:
            logger.error(f"Error in alert task: {e}", exc_info=True)
    
    @alert_task.before_loop
    async def before_alert_task(self):
        """Wait until bot is ready."""
        await self.bot.wait_until_ready()
    
    async def check_alerts(self):
        """Check all tickers for alerts."""
        if not self.bot.guilds:
            return
        
        # Get alerts channel from first guild (or config)
        guild = self.bot.guilds[0]
        alerts_channel = await self.get_channel(guild, "alerts")
        
        if not alerts_channel:
            logger.warning("Alerts channel not found")
            return
        
        # Check if market is open (best effort)
        tz = get_timezone(self.config.ALERT_TIMEZONE)
        if not is_market_open(tz):
            logger.debug("Market appears to be closed, skipping alerts")
            return
        
        for ticker in self.tickers:
            try:
                # Check if we've already alerted today
                if await self.db.has_alerted_today(ticker):
                    continue
                
                # Get quote
                quote = await self.market_data.get_quote(ticker)
                if not quote:
                    logger.warning(f"Failed to get quote for {ticker}")
                    continue
                
                percent_change = abs(quote["percent_change"])
                
                # Check threshold
                if percent_change >= self.config.ALERT_THRESHOLD_PERCENT:
                    # Send alert
                    await self.send_alert(alerts_channel, ticker, quote)
                    
                    # Record alert
                    await self.db.record_alert(ticker, quote["percent_change"])
                    
                    logger.info(f"Alert sent for {ticker}: {quote['percent_change']:.2f}%")
                    
            except Exception as e:
                logger.error(f"Error checking alert for {ticker}: {e}")
    
    async def send_alert(self, channel: discord.TextChannel, ticker: str, quote: dict):
        """Send price alert embed."""
        percent_change = quote["percent_change"]
        is_positive = percent_change >= 0
        emoji = "📈" if is_positive else "📉"
        color = discord.Color.green() if is_positive else discord.Color.red()
        
        embed = discord.Embed(
            title=f"{emoji} Price Alert: {ticker}",
            description=f"**{ticker}** has moved **{abs(percent_change):.2f}%** {'up' if is_positive else 'down'} today.",
            color=color,
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.add_field(name="Current Price", value=f"${quote['price']:.2f}", inline=True)
        embed.add_field(name="Previous Close", value=f"${quote['previous_close']:.2f}", inline=True)
        embed.add_field(name="Change", value=f"{percent_change:+.2f}%", inline=True)
        embed.add_field(name="Reason", value="(not available)", inline=False)
        
        embed.set_footer(text="Hidden Gems Research - The Gem Vault")
        
        try:
            await channel.send(embed=embed)
        except Exception as e:
            logger.error(f"Failed to send alert: {e}")
