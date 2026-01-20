"""Price alerts cog: regular-hours daily move monitoring + intraday RTH move alerts."""
import logging
from datetime import datetime, timezone, date
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from services.market_data import YahooFinanceProvider
from utils.time import parse_time, get_timezone, is_market_open

logger = logging.getLogger(__name__)


ET = ZoneInfo("America/New_York")


class AlertsCog(commands.Cog):
    """Price alerts functionality."""
    
    def __init__(self, bot):
        self.bot = bot
        self.config = bot.config
        self.db = bot.db
        self.market_data = YahooFinanceProvider()
        self.tickers = self.config.get_ticker_list()
        
        # Start daily close-to-close alerts only if enabled
        daily_enabled = bool(getattr(self.config, "ENABLE_DAILY_ALERTS", True))
        if daily_enabled:
            if not self.config.ALERT_CHECK_INTERVAL_MINUTES:
                # Scheduled time mode (default)
                self.alert_task.start()
            else:
                # Interval mode (legacy support)
                self.alert_task_interval.start()
    
        # Start intraday alert polling (RTH-only)
        if getattr(self.config, "ENABLE_INTRADAY_ALERTS", False):
            try:
                self.intraday_task.change_interval(seconds=float(self.config.INTRADAY_POLL_SECONDS))
            except Exception:
                # Keep default 60s if parsing fails
                pass
            self.intraday_task.start()

    def cog_unload(self):
        """Cleanup when cog is unloaded."""
        if self.alert_task.is_running():
            self.alert_task.cancel()
        if hasattr(self, "alert_task_interval") and self.alert_task_interval.is_running():
            self.alert_task_interval.cancel()
        if hasattr(self, "intraday_task") and self.intraday_task.is_running():
            self.intraday_task.cancel()
    
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
    
    def get_trading_date(self, dt: datetime) -> str:
        """Get trading date string (YYYY-MM-DD) in America/New_York timezone."""
        ny_time = dt.astimezone(ET)
        return ny_time.date().isoformat()

    # -----------------
    # Daily Alerts (existing behavior)
    # -----------------

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
    
    async def check_alerts(self, force: bool = False):
        """Check all tickers for daily close-to-close alerts."""
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
                prev_close = bars[1]["close"]  # Previous trading day
                
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
                        bars[0]["date"],
                    )
                    
                    # Update alert state
                    await self.db.update_alert_state(
                        ticker,
                        last_alert_date=trading_date,
                        last_alert_pct=pct_change,
                        last_run_at=datetime.now(timezone.utc),
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
                    last_run_at=datetime.now(timezone.utc),
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
                    timestamp=datetime.now(timezone.utc),
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
        trading_date: date,
    ):
        """Send price alert embed."""
        is_positive = pct_change >= 0
        color = discord.Color.green() if is_positive else discord.Color.red()
        
        embed = discord.Embed(
            title=f"🚨 10% Move Alert — ${ticker}",
            color=color,
            timestamp=datetime.now(timezone.utc),
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
        """Test daily alert check immediately."""
        await interaction.response.defer(ephemeral=True)
        
        try:
            await self.check_alerts(force=force)
            await interaction.followup.send(
                f"✅ Alert check completed. Force mode: {force}",
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"Error in alerts_test: {e}", exc_info=True)
            await interaction.followup.send(
                f"❌ Error running alert check: {str(e)}",
                ephemeral=True,
            )
    
    @app_commands.command(name="alerts_debug", description="Debug ticker data (admin only)")
    @app_commands.describe(ticker="Ticker symbol to debug")
    @app_commands.checks.has_permissions(administrator=True)
    async def alerts_debug(self, interaction: discord.Interaction, ticker: str):
        """Debug ticker data for daily alerts."""
        await interaction.response.defer(ephemeral=True)
        
        try:
            ticker = ticker.upper().strip()
            
            # Get daily bars
            bars = await self.market_data.get_daily_bars(ticker, days=10)
            if not bars or len(bars) < 2:
                await interaction.followup.send(
                    f"❌ Insufficient data for {ticker}. Need at least 2 trading days.",
                    ephemeral=True,
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
                timestamp=datetime.now(timezone.utc),
            )
            
            embed.add_field(
                name="Today's Close",
                value=f"${today_close:.2f} ({bars[0]['date']})",
                inline=False,
            )
            embed.add_field(
                name="Previous Close",
                value=f"${prev_close:.2f} ({bars[1]['date']})",
                inline=False,
            )
            embed.add_field(
                name="Percent Change",
                value=f"{pct_change:+.2f}%",
                inline=False,
            )
            embed.add_field(
                name="Threshold",
                value=f"±{self.config.ALERT_THRESHOLD_PERCENT}%",
                inline=False,
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
                if state.get("last_run_at"):
                    last_run = state["last_run_at"]
                    if isinstance(last_run, str):
                        last_run = datetime.fromisoformat(last_run)
                    state_text += f"Last Run: {last_run.strftime('%Y-%m-%d %H:%M UTC')}"
                embed.add_field(name="Alert State", value=state_text, inline=False)
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except Exception as e:
            logger.error(f"Error in alerts_debug: {e}", exc_info=True)
            await interaction.followup.send(
                f"❌ Error debugging {ticker}: {str(e)}",
                ephemeral=True,
            )

    # -----------------
    # Intraday Alerts (NEW behavior)
    # -----------------

    def _parse_intraday_thresholds(self) -> Tuple[float, float]:
        raw = getattr(self.config, "INTRADAY_THRESHOLDS", "5,10") or "5,10"
        try:
            parts = [float(p.strip()) for p in raw.split(",") if p.strip()]
        except Exception:
            parts = [5.0, 10.0]
        parts = sorted({abs(p) for p in parts if p > 0})
        if len(parts) >= 2:
            return parts[0], parts[1]
        if len(parts) == 1:
            return parts[0], parts[0] * 2
        return 5.0, 10.0

    def _zone_from_pct(self, pct: float, t1: float, t2: float) -> str:
        # Zones:
        # 0:  -t1 < pct < +t1
        # +t1: +t1 <= pct < +t2
        # -t1: -t2 < pct <= -t1
        # +t2: pct >= +t2
        # -t2: pct <= -t2
        if pct >= t2:
            return f"+{int(t2) if t2.is_integer() else t2}"
        if pct <= -t2:
            return f"-{int(t2) if t2.is_integer() else t2}"
        if pct >= t1:
            return f"+{int(t1) if t1.is_integer() else t1}"
        if pct <= -t1:
            return f"-{int(t1) if t1.is_integer() else t1}"
        return "0"

    def _zone_level(self, zone: str, t1: float, t2: float) -> int:
        z = (zone or "0").strip()
        zmap = {
            "0": 0,
            f"+{int(t1) if t1.is_integer() else t1}": 1,
            f"+{int(t2) if t2.is_integer() else t2}": 2,
            f"-{int(t1) if t1.is_integer() else t1}": -1,
            f"-{int(t2) if t2.is_integer() else t2}": -2,
        }
        return zmap.get(z, 0)

    def _should_send_zone_alert(self, prev_zone: str, curr_zone: str, t1: float, t2: float) -> bool:
        """
        Alert on "crossing into" a trigger zone, allowing repeats across the day, while
        suppressing down-crosses within the same sign (e.g., +10 -> +5).
        """
        prev_level = self._zone_level(prev_zone, t1, t2)
        curr_level = self._zone_level(curr_zone, t1, t2)

        if prev_level == curr_level:
            return False
        if curr_level == 0:
            return False  # never alert when entering zone 0

        # Always alert if magnitude increased (0->±5, ±5->±10)
        if abs(curr_level) > abs(prev_level):
            return True

        # Always alert if sign changed into a trigger zone (e.g., +5 -> -5)
        if prev_level == 0:
            return True
        if (prev_level > 0 and curr_level < 0) or (prev_level < 0 and curr_level > 0):
            return True

        # Otherwise it's a down-cross within the same sign (e.g., +10 -> +5), suppress.
        return False

    def _parse_db_timestamp(self, v) -> Optional[datetime]:
        if v is None:
            return None
        if isinstance(v, datetime):
            if v.tzinfo is None:
                return v.replace(tzinfo=timezone.utc)
            return v.astimezone(timezone.utc)
        if isinstance(v, str):
            try:
                dt = datetime.fromisoformat(v)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except Exception:
                return None
        return None

    def _alerts_role_mention(self, guild: discord.Guild) -> str:
        role_id = getattr(self.config, "ALERTS_ROLE_ID", None)
        if role_id:
            return f"<@&{role_id}>"

        name = (getattr(self.config, "ALERTS_ROLE_NAME", None) or "Alerts").strip()
        role = discord.utils.get(guild.roles, name=name)
        if not role:
            # Case-insensitive fallback
            name_cf = name.casefold()
            role = next((r for r in guild.roles if r.name.casefold() == name_cf), None)
        if role:
            return role.mention

        logger.warning("Alerts role not found; set ALERTS_ROLE_ID (preferred) or ALERTS_ROLE_NAME")
        return "@Alerts"

    async def send_intraday_alert(
        self,
        channel: discord.TextChannel,
        guild: discord.Guild,
        ticker: str,
        prev_close: float,
        current_price: float,
        pct_from_prev_close: float,
        threshold: float,
        as_of_et: datetime,
    ) -> None:
        """Send intraday alert embed and mention Alerts role."""
        is_positive = pct_from_prev_close >= 0
        color = discord.Color.green() if is_positive else discord.Color.red()

        embed = discord.Embed(
            title=f"🚨 {int(threshold) if float(threshold).is_integer() else threshold}% Move Alert — ${ticker}",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Price", value=f"${current_price:.2f}", inline=True)
        embed.add_field(name="Prev Close", value=f"${prev_close:.2f}", inline=True)
        embed.add_field(name="Move", value=f"{pct_from_prev_close:+.2f}%", inline=True)
        embed.add_field(name="Date", value=as_of_et.astimezone(ET).date().isoformat(), inline=True)
        embed.add_field(name="As of", value=as_of_et.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S ET"), inline=True)

        mention = self._alerts_role_mention(guild)
        await channel.send(
            content=mention,
            embed=embed,
            allowed_mentions=discord.AllowedMentions(roles=True),
        )

    @tasks.loop(seconds=60.0)
    async def intraday_task(self):
        """Intraday alert task - polls during RTH only."""
        try:
            if not getattr(self.config, "ENABLE_INTRADAY_ALERTS", False):
                return
            if not is_market_open(ET):
                return
            await self.check_intraday_alerts()
        except Exception as e:
            logger.error(f"Error in intraday task: {e}", exc_info=True)

    @intraday_task.before_loop
    async def before_intraday_task(self):
        """Wait until bot is ready."""
        await self.bot.wait_until_ready()

    async def check_intraday_alerts(self, force: bool = False, tickers: Optional[list[str]] = None) -> None:
        """Check tickers for intraday (RTH) threshold alerts vs previous RTH close."""
        if not self.bot.guilds:
            return

        guild = self.bot.guilds[0]
        alerts_channel = await self.get_channel(guild, "alerts")
        if not alerts_channel:
            logger.warning("Alerts channel not found (intraday)")
            return

        now_et = datetime.now(ET)
        trading_date = now_et.date().isoformat()
        t1, t2 = self._parse_intraday_thresholds()
        cooldown = int(getattr(self.config, "INTRADAY_ALERT_COOLDOWN_SECONDS", 120) or 120)

        for ticker in (tickers or self.tickers):
            ticker = ticker.upper().strip()
            try:
                state = await self.db.get_intraday_state(ticker, trading_date)
                prev_close = state.get("open_price") if state else None  # DB column reused as prev_close baseline
                alerted_5 = bool(state.get("alerted_5")) if state else False
                alerted_10 = bool(state.get("alerted_10")) if state else False
                last_alert_at = self._parse_db_timestamp(state.get("last_alert_at")) if state else None

                if not prev_close:
                    prev_close = await self.market_data.get_prev_rth_close(ticker, now_et.date())
                    if not prev_close:
                        logger.debug(f"[intraday] {ticker}: missing prev close baseline")
                        continue

                latest = await self.market_data.get_latest_rth_price_1m(ticker, timezone=ET)
                if not latest:
                    logger.debug(f"[intraday] {ticker}: missing latest 1m RTH price")
                    continue

                current_price, as_of_et = latest
                pct_from_prev_close = ((current_price - prev_close) / prev_close) * 100

                now_utc = datetime.now(timezone.utc)
                cooldown_ok = force or (not last_alert_at) or ((now_utc - last_alert_at).total_seconds() >= cooldown)

                new_last_alert_at = last_alert_at
                # Threshold logic: at most one 5% and one 10% alert per ticker per trading day
                did_alert = False
                threshold_to_send: Optional[float] = None
                if abs(pct_from_prev_close) >= t2 and (not alerted_10):
                    threshold_to_send = t2
                elif abs(pct_from_prev_close) >= t1 and (not alerted_5):
                    threshold_to_send = t1

                if threshold_to_send is not None and cooldown_ok:
                    await self.send_intraday_alert(
                        alerts_channel,
                        guild,
                        ticker,
                        prev_close=float(prev_close),
                        current_price=float(current_price),
                        pct_from_prev_close=float(pct_from_prev_close),
                        threshold=float(threshold_to_send),
                        as_of_et=as_of_et,
                    )
                    new_last_alert_at = now_utc
                    did_alert = True
                    if threshold_to_send == t1:
                        alerted_5 = True
                    if threshold_to_send == t2:
                        alerted_10 = True
                        # If we hit 10% first, consider 5% satisfied to avoid extra ping later.
                        alerted_5 = True
                    await self.db.record_intraday_alert_event(
                        ticker=ticker,
                        trading_date=trading_date,
                        zone=(f"+{threshold_to_send:.0f}" if pct_from_prev_close >= 0 else f"-{threshold_to_send:.0f}"),
                        pct=float(pct_from_prev_close),
                        price=float(current_price),
                        created_at=now_utc,
                    )
                    logger.info(f"[intraday] alert {ticker} threshold={threshold_to_send} pct={pct_from_prev_close:+.2f}%")
                elif threshold_to_send is not None and not cooldown_ok:
                    logger.debug(f"[intraday] cooldown suppress {ticker} threshold={threshold_to_send}")

                # Persist state every poll (regardless of whether we alert)
                # Note: DB column open_price is reused to store prev_close baseline for the day.
                await self.db.upsert_intraday_state(
                    ticker=ticker,
                    trading_date=trading_date,
                    open_price=float(prev_close),
                    last_price=float(current_price),
                    last_pct=float(pct_from_prev_close),
                    last_zone="0",
                    alerted_5=bool(alerted_5),
                    alerted_10=bool(alerted_10),
                    last_alert_at=new_last_alert_at,
                    updated_at=now_utc,
                )

            except Exception as e:
                logger.error(f"[intraday] error checking {ticker}: {e}", exc_info=True)

    @app_commands.command(name="intraday_test", description="Test intraday zone-crossing logic (admin only)")
    @app_commands.describe(
        ticker="Ticker symbol",
        open="RTH open price baseline",
        current="Current price",
        previous_zone="Optional previous zone (0, +5, -5, +10, -10)",
        force="Bypass cooldown",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def intraday_test(
        self,
        interaction: discord.Interaction,
        ticker: str,
        open: float,
        current: float,
        previous_zone: Optional[str] = None,
        force: bool = False,
    ):
        """Compute threshold logic vs previous close baseline; posts to #alerts when it should."""
        await interaction.response.defer(ephemeral=True)

        try:
            if not self.bot.guilds:
                await interaction.followup.send("❌ Bot is not in any guild.", ephemeral=True)
                return

            guild = self.bot.guilds[0]
            alerts_channel = await self.get_channel(guild, "alerts")
            if not alerts_channel:
                await interaction.followup.send("❌ Alerts channel not found.", ephemeral=True)
                return

            ticker = ticker.upper().strip()
            now_et = datetime.now(ET)
            trading_date = now_et.date().isoformat()
            t1, t2 = self._parse_intraday_thresholds()

            pct_from_prev_close = ((current - open) / open) * 100
            state = await self.db.get_intraday_state(ticker, trading_date)
            alerted_5 = bool(state.get("alerted_5")) if state else False
            alerted_10 = bool(state.get("alerted_10")) if state else False

            now_utc = datetime.now(timezone.utc)
            cooldown = int(getattr(self.config, "INTRADAY_ALERT_COOLDOWN_SECONDS", 120) or 120)
            last_alert_at = self._parse_db_timestamp(state.get("last_alert_at")) if state else None
            cooldown_ok = force or (not last_alert_at) or ((now_utc - last_alert_at).total_seconds() >= cooldown)

            did_post = False
            new_last_alert_at = last_alert_at
            threshold_to_send: Optional[float] = None
            if abs(pct_from_prev_close) >= t2 and (not alerted_10):
                threshold_to_send = t2
            elif abs(pct_from_prev_close) >= t1 and (not alerted_5):
                threshold_to_send = t1

            if threshold_to_send is not None and cooldown_ok:
                await self.send_intraday_alert(
                    alerts_channel,
                    guild,
                    ticker,
                    prev_close=float(open),
                    current_price=float(current),
                    pct_from_prev_close=float(pct_from_prev_close),
                    threshold=float(threshold_to_send),
                    as_of_et=now_et,
                )
                did_post = True
                new_last_alert_at = now_utc
                if threshold_to_send == t1:
                    alerted_5 = True
                if threshold_to_send == t2:
                    alerted_10 = True
                    alerted_5 = True
                await self.db.record_intraday_alert_event(
                    ticker=ticker,
                    trading_date=trading_date,
                    zone=(f"+{threshold_to_send:.0f}" if pct_from_prev_close >= 0 else f"-{threshold_to_send:.0f}"),
                    pct=float(pct_from_prev_close),
                    price=float(current),
                    created_at=now_utc,
                )

            await self.db.upsert_intraday_state(
                ticker=ticker,
                trading_date=trading_date,
                open_price=float(open),
                last_price=float(current),
                last_pct=float(pct_from_prev_close),
                last_zone="0",
                alerted_5=bool(alerted_5),
                alerted_10=bool(alerted_10),
                last_alert_at=new_last_alert_at,
                updated_at=now_utc,
            )

            await interaction.followup.send(
                f"ticker={ticker} prev_close={open:.2f} current={current:.2f} pct={pct_from_prev_close:+.2f}% "
                f"alerted_5={alerted_5} alerted_10={alerted_10} cooldown_ok={cooldown_ok} posted={did_post}",
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"Error in intraday_test: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

    @app_commands.command(name="intraday_debug", description="Debug intraday state for a ticker (admin only)")
    @app_commands.describe(ticker="Ticker symbol")
    @app_commands.checks.has_permissions(administrator=True)
    async def intraday_debug(self, interaction: discord.Interaction, ticker: str):
        """Show open, latest, pct, zone, last_zone, last_alert_at, cooldown, and whether market open."""
        await interaction.response.defer(ephemeral=True)

        try:
            ticker = ticker.upper().strip()
            now_et = datetime.now(ET)
            trading_date = now_et.date().isoformat()
            t1, t2 = self._parse_intraday_thresholds()
            cooldown = int(getattr(self.config, "INTRADAY_ALERT_COOLDOWN_SECONDS", 120) or 120)

            state = await self.db.get_intraday_state(ticker, trading_date)
            prev_close = state.get("open_price") if state else None  # DB column reused as prev_close baseline
            alerted_5 = bool(state.get("alerted_5")) if state else False
            alerted_10 = bool(state.get("alerted_10")) if state else False
            last_alert_at = self._parse_db_timestamp(state.get("last_alert_at")) if state else None

            latest = await self.market_data.get_latest_rth_price_1m(ticker, timezone=ET)
            current_price = None
            as_of_et = None
            if latest:
                current_price, as_of_et = latest

            pct_from_prev_close = None
            if prev_close and current_price:
                pct_from_prev_close = ((current_price - prev_close) / prev_close) * 100

            embed = discord.Embed(
                title=f"🧪 Intraday Debug — ${ticker}",
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Market Open (RTH)", value=str(is_market_open(ET)), inline=True)
            embed.add_field(name="Trading Date (ET)", value=trading_date, inline=True)
            embed.add_field(name="Cooldown (sec)", value=str(cooldown), inline=True)
            embed.add_field(name="Prev Close", value=f"${prev_close:.2f}" if prev_close else "N/A", inline=True)
            embed.add_field(name="Current (1m RTH)", value=f"${current_price:.2f}" if current_price else "N/A", inline=True)
            embed.add_field(name="Move", value=f"{pct_from_prev_close:+.2f}%" if pct_from_prev_close is not None else "N/A", inline=True)
            embed.add_field(name="Alerted 5%", value=str(alerted_5), inline=True)
            embed.add_field(name="Alerted 10%", value=str(alerted_10), inline=True)
            embed.add_field(
                name="Last Alert At (UTC)",
                value=last_alert_at.strftime("%Y-%m-%d %H:%M:%S UTC") if last_alert_at else "N/A",
                inline=False,
            )
            if as_of_et:
                embed.add_field(name="As of (ET)", value=as_of_et.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S ET"), inline=False)

            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Error in intraday_debug: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

    @app_commands.command(name="intraday_force_reset", description="Reset intraday state for today (admin only)")
    @app_commands.describe(ticker="Optional ticker to reset (otherwise resets all tracked tickers for today)")
    @app_commands.checks.has_permissions(administrator=True)
    async def intraday_force_reset(self, interaction: discord.Interaction, ticker: Optional[str] = None):
        """Reset intraday_state for today to allow re-testing."""
        await interaction.response.defer(ephemeral=True)

        try:
            now_et = datetime.now(ET)
            trading_date = now_et.date().isoformat()
            t = ticker.upper().strip() if ticker else None
            deleted = await self.db.delete_intraday_state(trading_date=trading_date, ticker=t)
            await interaction.followup.send(
                f"✅ Reset intraday state for trading_date={trading_date} ticker={t or 'ALL'} (rows deleted: {deleted})",
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"Error in intraday_force_reset: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)
