"""Main entry point for Hidden Gems Discord bot."""
import asyncio
import logging
import sys
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import Config
from db import Database
from utils.logging import setup_logging

# Import cogs
from cogs.onboarding import OnboardingCog
from cogs.admin_roles import AdminRolesCog
from cogs.verification_queue import VerificationQueueCog
from cogs.email_linking import EmailLinkingCog
from cogs.csv_import import CSVImportCog
from cogs.alerts import AlertsCog
from cogs.status import StatusCog
from cogs.access_panel import AccessPanelCog
from cogs.alerts_role_panel import AlertsRolePanelCog, AlertsRolePanelView
from cogs.substack import SubstackCog

logger = logging.getLogger(__name__)


class HiddenGemsBot(commands.Bot):
    """Main bot class."""
    
    def __init__(self, config: Config, db: Database):
        intents = discord.Intents.default()
        # These are privileged intents - MUST be enabled in Discord Developer Portal
        intents.members = True  # Required for role assignment on member join
        intents.message_content = True  # Required for reading message attachments in verification
        
        super().__init__(command_prefix=config.BOT_PREFIX, intents=intents)
        self.config = config
        self.db = db
        self.start_time = None
    
    async def setup_hook(self):
        """Called when bot is starting up."""
        logger.info("Setting up bot...")
        
        # Initialize database
        await self.db.init()
        
        # Load cogs
        await self.add_cog(OnboardingCog(self))
        await self.add_cog(AdminRolesCog(self))
        await self.add_cog(VerificationQueueCog(self))
        await self.add_cog(EmailLinkingCog(self))
        await self.add_cog(CSVImportCog(self))
        await self.add_cog(AlertsCog(self))
        await self.add_cog(StatusCog(self))
        await self.add_cog(AccessPanelCog(self))
        await self.add_cog(SubstackCog(self))
        await self.add_cog(AlertsRolePanelCog(self))
        
        # Register persistent views for Access Panel
        from cogs.access_panel import AccessPanelView
        self.add_view(AccessPanelView(self))
        # Register persistent views for Alerts Role Panel
        self.add_view(AlertsRolePanelView(self))
        logger.info("Registered persistent Access Panel views")
        
        logger.info("Bot setup complete")
    
    async def on_ready(self):
        """Called when bot is ready."""
        self.start_time = discord.utils.utcnow()
        logger.info(f"Bot ready! Logged in as {self.user}")
        logger.info(f"Guilds: {len(self.guilds)}")
        
        # Sync commands after bot is ready
        try:
            if self.config.GUILD_ID:
                # Check if bot is actually in the guild
                guild_obj = self.get_guild(self.config.GUILD_ID)
                if guild_obj:
                    logger.info(f"Syncing commands to guild {self.config.GUILD_ID} ({guild_obj.name})")
                    guild = discord.Object(id=self.config.GUILD_ID)
                    self.tree.copy_global_to(guild=guild)
                    synced = await self.tree.sync(guild=guild)
                    logger.info(f"Synced {len(synced)} commands to guild {self.config.GUILD_ID}")
                else:
                    logger.warning(f"Bot is not in guild {self.config.GUILD_ID}. Syncing global commands instead.")
                    synced = await self.tree.sync()
                    logger.info(f"Synced {len(synced)} global commands")
            else:
                logger.info("Syncing global commands...")
                synced = await self.tree.sync()
                logger.info(f"Synced {len(synced)} global commands")
        except discord.errors.Forbidden as e:
            logger.error(f"Failed to sync commands: {e}")
            logger.error("Make sure:")
            logger.error("1. Bot is invited with 'applications.commands' scope")
            logger.error("2. Bot has permission to create slash commands")
            logger.error("3. GUILD_ID is correct (if using guild-specific sync)")
            logger.error("Bot will continue running but commands may not be available.")
        except Exception as e:
            logger.error(f"Error syncing commands: {e}", exc_info=True)
    
    async def on_error(self, event, *args, **kwargs):
        """Global error handler."""
        logger.error(f"Error in event {event}", exc_info=True)


async def main():
    """Main entry point."""
    try:
        # Load config
        config = Config.from_env()
        
        if not config.DISCORD_TOKEN:
            print("ERROR: DISCORD_TOKEN not set in environment variables")
            print("Please set DISCORD_TOKEN in Railway environment variables")
            sys.exit(1)
        
        # Setup logging
        setup_logging(config.LOG_LEVEL)
        logger.info("Starting Hidden Gems bot...")
        
        # Initialize database
        if config.DATABASE_URL:
            logger.info("Initializing Postgres database")
            db = Database(database_url=config.DATABASE_URL)
        else:
            logger.info(f"Initializing SQLite database at {config.DB_PATH}")
            db = Database(db_path=config.DB_PATH)
        
        # Test database connection
        try:
            await db.init()
            logger.info("Database connection successful")
        except Exception as e:
            logger.error(f"Failed to connect to database: {e}")
            if config.DATABASE_URL:
                logger.error("Check that DATABASE_URL is correct and Postgres service is running")
            raise
        
        # Create and run bot
        bot = HiddenGemsBot(config, db)
        
        logger.info("Connecting to Discord...")
        await bot.start(config.DISCORD_TOKEN)
    except KeyboardInterrupt:
        logger.info("Bot shutting down...")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        print(f"FATAL ERROR: {e}")
        sys.exit(1)
    finally:
        if 'bot' in locals():
            await bot.close()
        if 'db' in locals():
            await db.close()


if __name__ == "__main__":
    asyncio.run(main())
