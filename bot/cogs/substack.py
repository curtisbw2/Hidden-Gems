"""Substack RSS monitoring cog: posts new Substack articles to Discord."""
import logging
from datetime import datetime, timezone
from typing import Optional
import asyncio
from time import mktime
import re

import discord
from discord.ext import commands, tasks

try:
    import feedparser
except ImportError:
    feedparser = None

logger = logging.getLogger(__name__)


class SubstackCog(commands.Cog):
    """Substack RSS monitoring functionality."""
    
    def __init__(self, bot):
        self.bot = bot
        self.config = bot.config
        self.db = bot.db
        self.last_check_time = None
        
        # Start monitoring task if RSS URL is configured
        if self.config.SUBSTACK_RSS_URL:
            if feedparser is None:
                logger.error("feedparser not installed. Install with: pip install feedparser")
            else:
                # Set loop interval from config
                self.monitor_task.change_interval(minutes=float(self.config.SUBSTACK_CHECK_INTERVAL_MINUTES))
                self.monitor_task.start()
        else:
            logger.warning("SUBSTACK_RSS_URL not configured, Substack monitoring disabled")
    
    def cog_unload(self):
        """Cleanup when cog is unloaded."""
        if self.config.SUBSTACK_RSS_URL:
            self.monitor_task.cancel()
    
    async def get_channel(self, guild: discord.Guild, channel_type: str) -> discord.TextChannel | None:
        """Get channel by type or ID."""
        channel_ids = self.config.get_channel_ids()
        channel_names = {
            "substack": self.config.CHANNEL_SUBSTACK,
            "alerts": self.config.CHANNEL_ALERTS,  # Fallback to alerts channel
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
    
    @tasks.loop(minutes=15.0)  # Default: check every 15 minutes
    async def monitor_task(self):
        """Monitor Substack RSS feed for new posts."""
        try:
            await self.check_substack_feed()
        except Exception as e:
            logger.error(f"Error in Substack monitor task: {e}", exc_info=True)
    
    @monitor_task.before_loop
    async def before_monitor_task(self):
        """Wait until bot is ready."""
        await self.bot.wait_until_ready()
        # Wait a bit more for guilds to be available
        await asyncio.sleep(5)
    
    async def check_substack_feed(self):
        """Check Substack RSS feed for new posts."""
        if not self.config.SUBSTACK_RSS_URL:
            return
        
        if feedparser is None:
            logger.error("feedparser not installed. Cannot check Substack feed.")
            return
        
        try:
            # Parse RSS feed
            feed = feedparser.parse(self.config.SUBSTACK_RSS_URL)
            
            if feed.bozo:
                logger.warning(f"RSS feed parsing error: {feed.bozo_exception}")
                return
            
            if not feed.entries:
                logger.debug("No entries in RSS feed")
                return
            
            # Get the most recent post timestamp we've seen
            last_seen_guid = await self.db.get_last_substack_post_guid()
            
            new_posts = []
            for entry in feed.entries:
                # Use GUID as unique identifier (Substack provides this)
                guid = entry.get("id") or entry.get("link")
                
                # Skip if we've already seen this post
                if last_seen_guid and guid == last_seen_guid:
                    break
                
                new_posts.append({
                    "guid": guid,
                    "title": entry.get("title", "Untitled"),
                    "link": entry.get("link", ""),
                    "published": entry.get("published_parsed"),
                    "summary": entry.get("summary", "")[:500] if entry.get("summary") else None,
                })
            
            # Process new posts in reverse order (oldest first)
            new_posts.reverse()
            
            if new_posts:
                logger.info(f"Found {len(new_posts)} new Substack post(s)")
                
                # Post to Discord
                for post in new_posts:
                    await self.post_to_discord(post)
                    
                    # Update last seen GUID
                    await self.db.set_last_substack_post_guid(post["guid"])
                    
                    # Small delay between posts
                    await asyncio.sleep(1)
            
            self.last_check_time = datetime.now(timezone.utc)
            
        except Exception as e:
            logger.error(f"Error checking Substack feed: {e}", exc_info=True)
    
    async def post_to_discord(self, post: dict):
        """Post a Substack article to Discord."""
        guild = None
        if self.config.GUILD_ID:
            guild = self.bot.get_guild(self.config.GUILD_ID)
        
        if not guild:
            # Try to get first available guild
            if self.bot.guilds:
                guild = self.bot.guilds[0]
            else:
                logger.warning("No guild available to post Substack update")
                return
        
        # Try substack channel first, fallback to alerts
        channel = await self.get_channel(guild, "substack")
        if not channel:
            channel = await self.get_channel(guild, "alerts")
        
        if not channel:
            logger.warning(f"Could not find channel to post Substack update in guild {guild.name}")
            return
        
        # Format published date
        published_str = "Recently"
        if post["published"]:
            try:
                published_dt = datetime.fromtimestamp(mktime(post["published"]), tz=timezone.utc)
                published_str = published_dt.strftime("%B %d, %Y")
            except Exception:
                pass
        
        # Create embed
        embed = discord.Embed(
            title="📰 New Substack Post",
            description=f"**{post['title']}**",
            url=post["link"],
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.add_field(name="Published", value=published_str, inline=True)
        embed.add_field(name="Link", value=f"[Read Article →]({post['link']})", inline=True)
        
        if post.get("summary"):
            # Clean up HTML tags from summary
            import re
            summary = re.sub(r'<[^>]+>', '', post["summary"])
            if len(summary) > 300:
                summary = summary[:300] + "..."
            embed.add_field(name="Preview", value=summary, inline=False)
        
        embed.set_footer(text="Hidden Gems Research")
        
        try:
            await channel.send(embed=embed)
            logger.info(f"Posted Substack article '{post['title']}' to {channel.name}")
        except Exception as e:
            logger.error(f"Failed to post Substack article to Discord: {e}", exc_info=True)
    
    @app_commands.command(name="check_substack", description="Manually check for new Substack posts (Admin only)")
    async def check_substack(self, interaction: discord.Interaction):
        """Manually trigger a Substack feed check."""
        # Check admin permissions
        if not interaction.guild:
            await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)
            return
        
        admin_cog = self.bot.get_cog("AdminRolesCog")
        if admin_cog:
            if not await admin_cog.check_mod_permissions(interaction):
                await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
                return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            await self.check_substack_feed()
            await interaction.followup.send("✅ Checked Substack feed for new posts.", ephemeral=True)
        except Exception as e:
            logger.error(f"Error in manual Substack check: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error checking feed: {str(e)}", ephemeral=True)
