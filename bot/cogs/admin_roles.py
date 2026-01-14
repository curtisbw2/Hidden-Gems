"""Admin role management cog."""
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)


class AdminRolesCog(commands.Cog):
    """Admin role management commands."""
    
    def __init__(self, bot):
        self.bot = bot
        self.config = bot.config
        self.db = bot.db
    
    async def get_role(self, guild: discord.Guild, role_type: str) -> discord.Role | None:
        """Get role by type or ID."""
        role_ids = self.config.get_role_ids()
        role_names = {
            "free": self.config.ROLE_FREE,
            "premium": self.config.ROLE_PREMIUM,
            "admin": self.config.ROLE_ADMIN,
            "mod": self.config.ROLE_MOD,
        }
        
        role_id = role_ids.get(role_type)
        if role_id:
            role = guild.get_role(role_id)
            if role:
                return role
        
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
    
    async def check_mod_permissions(self, interaction: discord.Interaction) -> bool:
        """Check if user has mod/admin permissions."""
        if not interaction.guild:
            return False
        
        member = interaction.user
        guild = interaction.guild
        
        # Check for Admin or Mod role
        admin_role = await self.get_role(guild, "admin")
        mod_role = await self.get_role(guild, "mod")
        
        if admin_role and admin_role in member.roles:
            return True
        if mod_role and mod_role in member.roles:
            return True
        
        # Fallback: check Discord permissions
        if member.guild_permissions.manage_roles:
            return True
        
        return False
    
    async def log_action(
        self, 
        guild: discord.Guild, 
        action: str, 
        moderator: discord.Member, 
        target: discord.Member,
        reason: str | None = None
    ):
        """Log action to bot-logs channel."""
        log_channel = await self.get_channel(guild, "bot_logs")
        if not log_channel:
            return
        
        embed = discord.Embed(
            title=f"🔧 {action}",
            description=(
                f"**Moderator:** {moderator.mention} ({moderator})\n"
                f"**Target:** {target.mention} ({target})\n"
                f"**User ID:** {target.id}"
            ),
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc)
        )
        
        if reason:
            embed.add_field(name="Reason", value=reason, inline=False)
        
        try:
            await log_channel.send(embed=embed)
        except Exception as e:
            logger.error(f"Failed to log action: {e}")
    
    @app_commands.command(name="grant_premium", description="Grant Premium role to a user (Mod/Admin only)")
    @app_commands.describe(user="The user to grant Premium role to", reason="Optional reason for logging")
    async def grant_premium(self, interaction: discord.Interaction, user: discord.Member, reason: str | None = None):
        """Grant Premium role."""
        if not await self.check_mod_permissions(interaction):
            await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("❌ This command can only be used in a server.", ephemeral=True)
            return
        
        premium_role = await self.get_role(guild, "premium")
        if not premium_role:
            await interaction.followup.send("❌ Premium Member role not found.", ephemeral=True)
            return
        
        if premium_role in user.roles:
            await interaction.followup.send(f"✅ {user.mention} already has the Premium role.", ephemeral=True)
            return
        
        try:
            await user.add_roles(premium_role, reason=reason or "Granted via /grant_premium")
            await interaction.followup.send(f"✅ Granted Premium role to {user.mention}", ephemeral=True)
            await self.log_action(guild, "Grant Premium", interaction.user, user, reason)
            logger.info(f"{interaction.user} granted premium to {user}")
        except discord.Forbidden:
            await interaction.followup.send("❌ I don't have permission to assign roles.", ephemeral=True)
        except Exception as e:
            logger.error(f"Error granting premium: {e}")
            await interaction.followup.send("❌ An error occurred.", ephemeral=True)
    
    @app_commands.command(name="revoke_premium", description="Revoke Premium role from a user (Mod/Admin only)")
    @app_commands.describe(user="The user to revoke Premium role from", reason="Optional reason for logging")
    async def revoke_premium(self, interaction: discord.Interaction, user: discord.Member, reason: str | None = None):
        """Revoke Premium role."""
        if not await self.check_mod_permissions(interaction):
            await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("❌ This command can only be used in a server.", ephemeral=True)
            return
        
        premium_role = await self.get_role(guild, "premium")
        if not premium_role:
            await interaction.followup.send("❌ Premium Member role not found.", ephemeral=True)
            return
        
        if premium_role not in user.roles:
            await interaction.followup.send(f"✅ {user.mention} doesn't have the Premium role.", ephemeral=True)
            return
        
        try:
            await user.remove_roles(premium_role, reason=reason or "Revoked via /revoke_premium")
            await interaction.followup.send(f"✅ Revoked Premium role from {user.mention}", ephemeral=True)
            await self.log_action(guild, "Revoke Premium", interaction.user, user, reason)
            logger.info(f"{interaction.user} revoked premium from {user}")
        except discord.Forbidden:
            await interaction.followup.send("❌ I don't have permission to manage roles.", ephemeral=True)
        except Exception as e:
            logger.error(f"Error revoking premium: {e}")
            await interaction.followup.send("❌ An error occurred.", ephemeral=True)
    
    @app_commands.command(name="grant_free", description="Grant Free role to a user (Mod/Admin only)")
    @app_commands.describe(user="The user to grant Free role to")
    async def grant_free(self, interaction: discord.Interaction, user: discord.Member):
        """Grant Free role."""
        if not await self.check_mod_permissions(interaction):
            await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("❌ This command can only be used in a server.", ephemeral=True)
            return
        
        free_role = await self.get_role(guild, "free")
        if not free_role:
            await interaction.followup.send("❌ Free Member role not found.", ephemeral=True)
            return
        
        if free_role in user.roles:
            await interaction.followup.send(f"✅ {user.mention} already has the Free role.", ephemeral=True)
            return
        
        try:
            await user.add_roles(free_role, reason="Granted via /grant_free")
            await interaction.followup.send(f"✅ Granted Free role to {user.mention}", ephemeral=True)
            await self.log_action(guild, "Grant Free", interaction.user, user)
            logger.info(f"{interaction.user} granted free to {user}")
        except discord.Forbidden:
            await interaction.followup.send("❌ I don't have permission to assign roles.", ephemeral=True)
        except Exception as e:
            logger.error(f"Error granting free: {e}")
            await interaction.followup.send("❌ An error occurred.", ephemeral=True)
    
    @app_commands.command(name="revoke_free", description="Revoke Free role from a user (Mod/Admin only)")
    @app_commands.describe(user="The user to revoke Free role from")
    async def revoke_free(self, interaction: discord.Interaction, user: discord.Member):
        """Revoke Free role."""
        if not await self.check_mod_permissions(interaction):
            await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("❌ This command can only be used in a server.", ephemeral=True)
            return
        
        free_role = await self.get_role(guild, "free")
        if not free_role:
            await interaction.followup.send("❌ Free Member role not found.", ephemeral=True)
            return
        
        if free_role not in user.roles:
            await interaction.followup.send(f"✅ {user.mention} doesn't have the Free role.", ephemeral=True)
            return
        
        try:
            await user.remove_roles(free_role, reason="Revoked via /revoke_free")
            await interaction.followup.send(f"✅ Revoked Free role from {user.mention}", ephemeral=True)
            await self.log_action(guild, "Revoke Free", interaction.user, user)
            logger.info(f"{interaction.user} revoked free from {user}")
        except discord.Forbidden:
            await interaction.followup.send("❌ I don't have permission to manage roles.", ephemeral=True)
        except Exception as e:
            logger.error(f"Error revoking free: {e}")
            await interaction.followup.send("❌ An error occurred.", ephemeral=True)
    
    @app_commands.command(name="whois", description="Get user information (Mod/Admin only)")
    @app_commands.describe(user="The user to look up")
    async def whois(self, interaction: discord.Interaction, user: discord.Member):
        """Get user information."""
        if not await self.check_mod_permissions(interaction):
            await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("❌ This command can only be used in a server.", ephemeral=True)
            return
        
        # Get user data
        user_data = await self.db.get_user(user.id)
        
        # Get roles
        premium_role = await self.get_role(guild, "premium")
        free_role = await self.get_role(guild, "free")
        
        has_premium = premium_role and premium_role in user.roles
        has_free = free_role and free_role in user.roles
        
        # Build embed
        embed = discord.Embed(
            title=f"👤 User Info: {user.display_name}",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.add_field(name="User ID", value=str(user.id), inline=True)
        embed.add_field(name="Username", value=str(user), inline=True)
        embed.add_field(name="Joined Server", value=user.joined_at.strftime("%Y-%m-%d %H:%M UTC") if user.joined_at else "Unknown", inline=True)
        
        # Roles
        role_names = [r.name for r in user.roles if r.name != "@everyone"]
        embed.add_field(name="Roles", value=", ".join(role_names[:10]) or "None", inline=False)
        
        # Premium status
        premium_status = "✅ Yes" if has_premium else "❌ No"
        embed.add_field(name="Premium Role", value=premium_status, inline=True)
        
        # Free status
        free_status = "✅ Yes" if has_free else "❌ No"
        embed.add_field(name="Free Role", value=free_status, inline=True)
        
        # Email verification
        if user_data:
            email_verified = "✅ Yes" if user_data.get("email_verified") else "❌ No"
            linked_at = user_data.get("linked_at")
            if linked_at:
                linked_at_str = datetime.fromisoformat(linked_at).strftime("%Y-%m-%d %H:%M UTC")
            else:
                linked_at_str = "Never"
            
            embed.add_field(name="Email Verified", value=email_verified, inline=True)
            embed.add_field(name="Email Linked At", value=linked_at_str, inline=True)
        else:
            embed.add_field(name="Email Verified", value="❌ No", inline=True)
            embed.add_field(name="Email Linked At", value="Never", inline=True)
        
        await interaction.followup.send(embed=embed, ephemeral=True)
