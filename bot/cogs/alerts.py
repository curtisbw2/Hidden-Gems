"""Price alerts cog: regular-hours daily move monitoring."""
import logging
from datetime import datetime, timezone, date
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from services.market_data import YahooFinanceProvider
from utils.time import parse_time, get_timezone

logger = logging.getLogger(__name__)


class AlertsCog(commands.Cog):
    """Price alerts functionality."""
    
    def __init__(self, bot):
        self.bot = bot
        self.config = bot.config
        self.db = bot.db
        self.market_data = YahooFinanceProvider()
        self.tickers = self.config.get_ticker_list()
        
        # Start alert task
        if not self.config.ALERT_CHECK_INTERVAL_MINUTES:
            # Scheduled time mode (default)
            self.alert_task.start()
        else:
            # Interval mode (legacy support)
            self.alert_task_interval.start()
    
    def cog_unload(self):
        """Cleanup when cog is unloaded."""
        if self.alert_task.is_running():
            self.alert_task.cancel()
        if hasattr(self, 'alert_task_interval') and self.alert_task_interval.is_running():
            self.alert_task_interval.cancel()
    
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
        """Scheduled alert task - runs at configured time daily."""
        try:
            tz = get_timezone(self.config.ALERT_TIMEZONE)
            target_time = parse_time(self.config.ALERT_TIME)
            now = datetime.now(tz)
            
            # Check if current time matches target time (within 1 minute window)
            if now.hour == target_time.hour and now.minute == target_time.minute:
                await self.check_alerts()
        except Exception as e:
            logger.error(f"Error in alert task: {e}", exc_info=True)
    
    @tasks.loop(minutes=1.0)
    async def alert_task_interval(self):
        """Interval-based alert task (legacy support)."""
        try:
            # Check if we've already run today
            last_run = await self.db.get_last_alert_run_time()
            if last_run:
                now = datetime.now(timezone.utc)
                if (now - last_run).total_seconds() < 3600:  # Less than 1 hour ago
                    return
            
            await self.check_alerts()
        except Exception as e:
            logger.error(f"Error in alert task interval: {e}", exc_info=True)
    
    @alert_task.before_loop
    async def before_alert_task(self):
        """Wait until bot is ready."""
        await self.bot.wait_until_ready()
    
    @alert_task_interval.before_loop
    async def before_alert_task_interval(self):
        """Wait until bot is ready."""
        await self.bot.wait_until_ready()
    
    def get_trading_date(self, dt: datetime) -> str:
        """Get trading date string (YYYY-MM-DD) in America/New_York timezone."""
        tz = get_timezone("America/New_York")
        ny_time = dt.astimezone(tz)
        return ny_time.date().isoformat()
    
    async def check_alerts(self, force: bool = False):
        """Check all tickers for alerts."""
        if not self.bot.guilds:
            return
        
        guild = self.bot.guilds[0]
        alerts_channel = await self.get_channel(guild, "alerts")
        logs_channel = await self.get_channel(guild, "bot_logs")
        
        if not alerts_channel:
            logger.warning("Alerts channel not found")
            return
        
        tz = get_timezone(self.config.ALERT_TIMEZONE)
        now = datetime.now(tz)
        trading_date = self.get_trading_date(now)
        
        success_count = 0
        alert_count = 0
        error_tickers = []
        
        # Cache daily bars per ticker to minimize API calls
        bars_cache = {}
        
        for ticker in self.tickers:
            try:
                # Check if we've already alerted today (unless force)
                if not force:
                    state = await self.db.get_alert_state(ticker)
                    if state and state.get("last_alert_date") == trading_date:
                        logger.debug(f"Skipping {ticker}: already alerted today ({trading_date})")
                        continue
                
                # Get daily bars (cached)
                if ticker not in bars_cache:
                    bars = await self.market_data.get_daily_bars(ticker, days=10)
                    if not bars or len(bars) < 2:
                        logger.warning(f"Insufficient data for {ticker}: need at least 2 trading days")
                        error_tickers.append(f"{ticker}: insufficient data")
                        continue
                    bars_cache[ticker] = bars
                else:
                    bars = bars_cache[ticker]
                
                # Get today's close and previous close
                today_close = bars[0]["close"]  # Most recent bar
                prev_close = bars[1]["close"]   # Previous trading day
                
                # Calculate percent change
                pct_change = ((today_close - prev_close) / prev_close) * 100
                
                # Check threshold
                if abs(pct_change) >= self.config.ALERT_THRESHOLD_PERCENT:
                    # Send alert
                    await self.send_alert(
                        alerts_channel, 
                        ticker, 
                        today_close, 
                        prev_close, 
                        pct_change,
                        bars[0]["date"]
                    )
                    
                    # Update alert state
                    await self.db.update_alert_state(
                        ticker,
                        last_alert_date=trading_date,
                        last_alert_pct=pct_change,
                        last_run_at=datetime.now(timezone.utc)
                    )
                    
                    # Also record in alert_history
                    await self.db.record_alert(ticker, pct_change)
                    
                    alert_count += 1
                    logger.info(f"Alert sent for {ticker}: {pct_change:.2f}%")
                
                # Update last_run_at even if no alert
                await self.db.update_alert_state(
                    ticker,
                    last_alert_date=None,  # Don't change if no alert
                    last_alert_pct=None,
                    last_run_at=datetime.now(timezone.utc)
                )
                
                success_count += 1
                
            except Exception as e:
                logger.error(f"Error checking alert for {ticker}: {e}", exc_info=True)
                error_tickers.append(f"{ticker}: {str(e)[:50]}")
        
        # Log summary to bot-logs
        if logs_channel:
            try:
                embed = discord.Embed(
                    title="📊 Alert Check Summary",
                    color=discord.Color.blue(),
                    timestamp=datetime.now(timezone.utc)
                )
                embed.add_field(name="Tickers Checked", value=str(success_count), inline=True)
                embed.add_field(name="Alerts Sent", value=str(alert_count), inline=True)
                embed.add_field(name="Errors", value=str(len(error_tickers)), inline=True)
                
                if error_tickers:
                    error_text = "\n".join(error_tickers[:10])  # Limit to 10
                    if len(error_tickers) > 10:
                        error_text += f"\n... and {len(error_tickers) - 10} more"
                    embed.add_field(name="Error Details", value=f"```{error_text}```", inline=False)
                
                await logs_channel.send(embed=embed)
            except Exception as e:
                logger.error(f"Failed to send summary to bot-logs: {e}")
    
    async def send_alert(
        self, 
        channel: discord.TextChannel, 
        ticker: str, 
        today_close: float,
        prev_close: float,
        pct_change: float,
        trading_date: date
    ):
        """Send price alert embed."""
        is_positive = pct_change >= 0
        color = discord.Color.green() if is_positive else discord.Color.red()
        
        embed = discord.Embed(
            title=f"🚨 10% Move Alert — ${ticker}",
            color=color,
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.add_field(name="Price", value=f"${today_close:.2f}", inline=True)
        embed.add_field(name="Prev Close", value=f"${prev_close:.2f}", inline=True)
        embed.add_field(name="Move", value=f"{pct_change:+.2f}%", inline=True)
        embed.add_field(name="Date", value=trading_date.isoformat(), inline=True)
        embed.add_field(name="As of", value=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), inline=True)
        
        embed.set_footer(text="Hidden Gems Research - The Gem Vault")
        
        try:
            await channel.send(embed=embed)
        except Exception as e:
            logger.error(f"Failed to send alert: {e}")
    
    @app_commands.command(name="alerts_test", description="Test alert check (admin only)")
    @app_commands.describe(force="Bypass once-per-day check")
    @app_commands.checks.has_permissions(administrator=True)
    async def alerts_test(self, interaction: discord.Interaction, force: bool = False):
        """Test alert check immediately."""
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Check prerequisites
            if not self.bot.guilds:
                await interaction.followup.send(
                    "❌ Bot is not in any guilds.",
                    ephemeral=True
                )
                return
            
            guild = interaction.guild or self.bot.guilds[0]
            
            # Check if alerts channel exists
            alerts_channel = await self.get_channel(guild, "alerts")
            if not alerts_channel:
                channel_id = self.config.get_channel_ids().get("alerts")
                channel_name = self.config.CHANNEL_ALERTS
                await interaction.followup.send(
                    f"❌ Alerts channel not found!\n"
                    f"• Looking for ID: {channel_id or 'Not set'}\n"
                    f"• Looking for name: #{channel_name}\n"
                    f"• Set `CHANNEL_ALERTS_ID` environment variable to: 1460785397079736420",
                    ephemeral=True
                )
                return
            
            # Respond immediately, then run check in background
            await interaction.followup.send(
                f"🔄 Starting alert check...\n"
                f"• Force mode: {force}\n"
                f"• Tickers: {len(self.tickers)}\n"
                f"• This may take a moment. Check `#alerts` and `#bot-logs` for results.",
                ephemeral=True
            )
            
            # Run check in background task to avoid timeout
            import asyncio
            asyncio.create_task(self.check_alerts(force=force))
        except Exception as e:
            logger.error(f"Error in alerts_test: {e}", exc_info=True)
            await interaction.followup.send(
                f"❌ Error running alert check: {str(e)}\n"
                f"Check bot logs for details.",
                ephemeral=True
            )
    
    @app_commands.command(name="alerts_debug", description="Debug ticker data (admin only)")
    @app_commands.describe(ticker="Ticker symbol to debug")
    @app_commands.checks.has_permissions(administrator=True)
    async def alerts_debug(self, interaction: discord.Interaction, ticker: str):
        """Debug ticker data."""
        await interaction.response.defer(ephemeral=True)
        
        try:
            ticker = ticker.upper().strip()
            
            # Get daily bars
            bars = await self.market_data.get_daily_bars(ticker, days=10)
            if not bars or len(bars) < 2:
                await interaction.followup.send(
                    f"❌ Insufficient data for {ticker}. Need at least 2 trading days.",
                    ephemeral=True
                )
                return
            
            # Calculate percent change
            today_close = bars[0]["close"]
            prev_close = bars[1]["close"]
            pct_change = ((today_close - prev_close) / prev_close) * 100
            
            # Build debug embed
            embed = discord.Embed(
                title=f"🔍 Debug: ${ticker}",
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc)
            )
            
            embed.add_field(
                name="Today's Close",
                value=f"${today_close:.2f} ({bars[0]['date']})",
                inline=False
            )
            embed.add_field(
                name="Previous Close",
                value=f"${prev_close:.2f} ({bars[1]['date']})",
                inline=False
            )
            embed.add_field(
                name="Percent Change",
                value=f"{pct_change:+.2f}%",
                inline=False
            )
            embed.add_field(
                name="Threshold",
                value=f"±{self.config.ALERT_THRESHOLD_PERCENT}%",
                inline=False
            )
            
            # Show last 5 bars
            bars_text = "```\n"
            for bar in bars[:5]:
                bars_text += f"{bar['date']}: ${bar['close']:.2f}\n"
            bars_text += "```"
            embed.add_field(name="Recent Bars (last 5)", value=bars_text, inline=False)
            
            # Get alert state
            state = await self.db.get_alert_state(ticker)
            if state:
                state_text = f"Last Alert Date: {state.get('last_alert_date', 'Never')}\n"
                state_text += f"Last Alert %: {state.get('last_alert_pct', 'N/A')}\n"
                if state.get('last_run_at'):
                    last_run = state['last_run_at']
                    if isinstance(last_run, str):
                        last_run = datetime.fromisoformat(last_run)
                    state_text += f"Last Run: {last_run.strftime('%Y-%m-%d %H:%M UTC')}"
                embed.add_field(name="Alert State", value=state_text, inline=False)
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except Exception as e:
            logger.error(f"Error in alerts_debug: {e}", exc_info=True)
            await interaction.followup.send(
                f"❌ Error debugging {ticker}: {str(e)}",
                ephemeral=True
            )
