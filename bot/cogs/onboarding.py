"""Onboarding cog: welcome messages, free role assignment, /start command."""
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)


class OnboardingButtons(discord.ui.View):
    """Buttons for onboarding."""
    
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot
    
    @discord.ui.button(label="Get Free Access", style=discord.ButtonStyle.primary, emoji="🆓")
    async def get_free(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Assign free role."""
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.errors.InteractionResponded:
            # Already responded, try followup
            pass
        
        guild = interaction.guild
        if not guild:
            try:
                await interaction.followup.send("❌ This command can only be used in a server.", ephemeral=True)
            except:
                await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)
            return
        
        # Get member object (not just user)
        member = guild.get_member(interaction.user.id)
        if not member:
            try:
                await interaction.followup.send("❌ Could not find your member information. Please try again.", ephemeral=True)
            except:
                await interaction.response.send_message("❌ Could not find your member information. Please try again.", ephemeral=True)
            return
        
        # Get free role
        onboarding_cog = self.bot.get_cog("OnboardingCog")
        if not onboarding_cog:
            try:
                await interaction.followup.send("❌ Bot configuration error. Please contact an admin.", ephemeral=True)
            except:
                await interaction.response.send_message("❌ Bot configuration error. Please contact an admin.", ephemeral=True)
            return
        
        free_role = await onboarding_cog.get_role(guild, "free")
        if not free_role:
            try:
                await interaction.followup.send("❌ Free Member role not found. Please contact an admin.", ephemeral=True)
            except:
                await interaction.response.send_message("❌ Free Member role not found. Please contact an admin.", ephemeral=True)
            return
        
        if free_role in member.roles:
            try:
                await interaction.followup.send("✅ You already have the Free Member role!", ephemeral=True)
            except:
                await interaction.response.send_message("✅ You already have the Free Member role!", ephemeral=True)
            return
        
        try:
            await member.add_roles(free_role, reason="Onboarding: Get Free Access button")
            try:
                await interaction.followup.send("✅ You've been granted Free Member access!", ephemeral=True)
            except:
                await interaction.response.send_message("✅ You've been granted Free Member access!", ephemeral=True)
            logger.info(f"Assigned free role to {member} via button")
        except discord.Forbidden:
            error_msg = "❌ I don't have permission to assign roles. Please ensure the bot role is above the Free Member role."
            try:
                await interaction.followup.send(error_msg, ephemeral=True)
            except:
                await interaction.response.send_message(error_msg, ephemeral=True)
            logger.error(f"Forbidden error assigning free role to {member}")
        except Exception as e:
            logger.error(f"Error assigning free role: {e}", exc_info=True)
            error_msg = f"❌ An error occurred: {str(e)}. Please contact an admin."
            try:
                await interaction.followup.send(error_msg, ephemeral=True)
            except:
                try:
                    await interaction.response.send_message(error_msg, ephemeral=True)
                except:
                    pass
    
    @discord.ui.button(label="Verify Premium (Mod Queue)", style=discord.ButtonStyle.secondary, emoji="💎")
    async def verify_premium_info(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show info about premium verification."""
        await interaction.response.send_message(
            "💎 **Premium Verification via Mod Queue**\n\n"
            "Use `/verify_premium` in the #verify channel to submit your Substack subscription proof.\n"
            "A moderator will review your request and approve or reject it.\n\n"
            "**Note:** This requires uploading a screenshot of your subscription.",
            ephemeral=True
        )
    
    @discord.ui.button(label="Link Email (No Screenshot)", style=discord.ButtonStyle.secondary, emoji="📧")
    async def link_email_info(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show info about email linking."""
        await interaction.response.send_message(
            "📧 **Email Linking (No Screenshot Required)**\n\n"
            "Use `/link_email` to link your Substack email address.\n"
            "You'll receive a verification code via email, then use `/confirm_code` to complete the process.\n\n"
            "**Note:** Premium access will be granted automatically if your email is in our paid subscriber list.",
            ephemeral=True
        )


class OnboardingCog(commands.Cog):
    """Onboarding functionality."""
    
    def __init__(self, bot):
        self.bot = bot
        self.config = bot.config
        self.db = bot.db
    
    async def get_role(self, guild: discord.Guild, role_type: str) -> discord.Role | None:
        """Get role by type (free, premium, admin, mod) or ID."""
        role_ids = self.config.get_role_ids()
        role_names = {
            "free": self.config.ROLE_FREE,
            "premium": self.config.ROLE_PREMIUM,
            "admin": self.config.ROLE_ADMIN,
            "mod": self.config.ROLE_MOD,
        }
        
        # Try ID first
        role_id = role_ids.get(role_type)
        if role_id:
            role = guild.get_role(role_id)
            if role:
                return role
        
        # Try name
        role_name = role_names.get(role_type)
        if role_name:
            role = discord.utils.get(guild.roles, name=role_name)
            if role:
                return role
        
        return None
    
    async def get_channel(self, guild: discord.Guild, channel_type: str) -> discord.TextChannel | None:
        """Get channel by type or ID."""
        channel_ids = self.config.get_channel_ids()
        channel_names = {
            "verify": self.config.CHANNEL_VERIFY,
            "verify_queue": self.config.CHANNEL_VERIFY_QUEUE,
            "bot_logs": self.config.CHANNEL_BOT_LOGS,
            "alerts": self.config.CHANNEL_ALERTS,
        }
        
        # Try ID first
        channel_id = channel_ids.get(channel_type)
        if channel_id:
            channel = guild.get_channel(channel_id)
            if channel and isinstance(channel, discord.TextChannel):
                return channel
        
        # Try name
        channel_name = channel_names.get(channel_type)
        if channel_name:
            channel = discord.utils.get(guild.text_channels, name=channel_name)
            if channel:
                return channel
        
        return None
    
    async def send_welcome_dm(self, member: discord.Member) -> bool:
        """Send welcome DM to new member. Returns True if successful."""
        try:
            embed = discord.Embed(
                title="💎 Welcome to Hidden Gems Research – The Gem Vault!",
                description=(
                    "Welcome to our options trading community!\n\n"
                    "**Getting Started:**\n"
                    "1. Read the server rules\n"
                    "2. You have **Free Member** access\n"
                    "3. To upgrade to **Premium Member**:\n"
                    "   • Use `/verify_premium` in #verify (requires screenshot)\n"
                    "   • Or use `/link_email` + `/confirm_code` (no screenshot)\n\n"
                    "**Important:** This bot and community do not provide financial advice. "
                    "All trading decisions are your own responsibility.\n\n"
                    "Use `/start` in the server for an interactive guide!"
                ),
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc)
            )
            
            await member.send(embed=embed)
            return True
        except discord.Forbidden:
            logger.warning(f"Could not DM {member}: DMs disabled")
            return False
        except Exception as e:
            logger.error(f"Error sending welcome DM to {member}: {e}")
            return False
    
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Handle new member join."""
        guild = member.guild
        
        # Create user record
        await self.db.create_user(member.id)
        
        # Auto-assign free role if enabled
        if self.config.AUTO_ASSIGN_FREE_ON_JOIN:
            free_role = await self.get_role(guild, "free")
            if free_role:
                try:
                    await member.add_roles(free_role, reason="Auto-assign on join")
                    logger.info(f"Auto-assigned free role to {member}")
                except Exception as e:
                    logger.error(f"Failed to auto-assign free role to {member}: {e}")
        
        # Send welcome DM
        dm_sent = await self.send_welcome_dm(member)
        
        # If DM failed, try fallback channel
        if not dm_sent and self.config.CHANNEL_FALLBACK_DM:
            fallback_channel = guild.get_channel(self.config.CHANNEL_FALLBACK_DM)
            if fallback_channel and isinstance(fallback_channel, discord.TextChannel):
                try:
                    await fallback_channel.send(
                        f"{member.mention} Welcome! Check your DMs for onboarding instructions. "
                        "If you didn't receive a DM, use `/start` for a guide."
                    )
                except Exception as e:
                    logger.error(f"Failed to send fallback message: {e}")
    
    @app_commands.command(name="start", description="Get started with Hidden Gems Research")
    async def start(self, interaction: discord.Interaction):
        """Onboarding command with buttons."""
        embed = discord.Embed(
            title="💎 Hidden Gems Research – The Gem Vault",
            description=(
                "**Welcome! Here's how to get started:**\n\n"
                "**1. Free Access**\n"
                "Click the button below to get Free Member access.\n\n"
                "**2. Premium Access**\n"
                "Choose one of two methods:\n"
                "• **Mod Queue**: Submit proof via `/verify_premium`\n"
                "• **Email Link**: Use `/link_email` + `/confirm_code`\n\n"
                "**⚠️ Important Disclaimer**\n"
                "This bot and community do not provide financial advice. "
                "All trading decisions are your own responsibility. "
                "The bot only automates server administration tasks."
            ),
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )
        
        view = OnboardingButtons(self.bot)
        await interaction.response.send_message(embed=embed, view=view)
