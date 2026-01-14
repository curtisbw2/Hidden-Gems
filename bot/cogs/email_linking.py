"""Email linking cog: OTP verification."""
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from services.hashing import hash_email, normalize_email, generate_otp_code, hash_otp_code
from services.email_service import EmailService

logger = logging.getLogger(__name__)


class EmailLinkingCog(commands.Cog):
    """Email linking functionality."""
    
    def __init__(self, bot):
        self.bot = bot
        self.config = bot.config
        self.db = bot.db
        self.email_service = EmailService(self.config.SENDGRID_API_KEY, self.config.FROM_EMAIL)
    
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
    
    @app_commands.command(name="link_email", description="Link your Substack email address")
    @app_commands.describe(email="Your Substack email address")
    async def link_email(self, interaction: discord.Interaction, email: str):
        """Link email address."""
        await interaction.response.defer(ephemeral=True)
        
        if not self.email_service.enabled:
            await interaction.followup.send(
                "❌ Email service is not configured. Please contact an administrator.",
                ephemeral=True
            )
            return
        
        # Normalize and hash email
        normalized = normalize_email(email)
        email_hash = hash_email(normalized)
        
        # Check if email is already linked to another user
        existing_user = await self.db.get_user_by_email_hash(email_hash)
        if existing_user and existing_user["discord_user_id"] != interaction.user.id:
            await interaction.followup.send(
                "❌ This email is already linked to another Discord account.",
                ephemeral=True
            )
            return
        
        # Generate OTP
        otp_code, otp_hash = generate_otp_code()
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=self.config.OTP_EXPIRY_MINUTES)
        
        # Store OTP with email hash
        await self.db.store_otp(interaction.user.id, otp_hash, email_hash, expires_at)
        
        # Send email
        email_sent = await self.email_service.send_otp(normalized, otp_code)
        
        if not email_sent:
            await interaction.followup.send(
                "❌ Failed to send verification email. Please try again later or contact support.",
                ephemeral=True
            )
            return
        
        # Store email hash temporarily (we'll link it after OTP confirmation)
        # Actually, we don't need to store it - we'll hash the same way in confirm_code
        
        await interaction.followup.send(
            f"✅ Verification code sent to your email! Run `/confirm_code <code> <email>` to complete the process.\n\n"
            f"**Note:** The code expires in {self.config.OTP_EXPIRY_MINUTES} minutes.",
            ephemeral=True
        )
    
    @app_commands.command(name="confirm_code", description="Confirm your email with the verification code")
    @app_commands.describe(code="The 6-digit verification code sent to your email", email="Your email address")
    async def confirm_code(self, interaction: discord.Interaction, code: str, email: str):
        """Confirm OTP code."""
        await interaction.response.defer(ephemeral=True)
        
        # Validate code format
        if not code.isdigit() or len(code) != 6:
            await interaction.followup.send("❌ Invalid code format. Please enter a 6-digit number.", ephemeral=True)
            return
        
        # Normalize and hash email
        normalized = normalize_email(email)
        email_hash = hash_email(normalized)
        
        # Hash code
        code_hash = hash_otp_code(code)
        
        # Get OTP record (try by code_hash first, then by email_hash)
        otp_record = await self.db.get_otp(interaction.user.id, code_hash)
        if not otp_record:
            # Try by email_hash
            otp_record = await self.db.get_otp_by_email_hash(interaction.user.id, email_hash)
            if not otp_record:
                # Increment attempts for any OTP (to prevent brute force)
                await self.db.increment_otp_attempts(interaction.user.id, code_hash)
                await interaction.followup.send(
                    "❌ Invalid or expired code. Please request a new code with `/link_email`.",
                    ephemeral=True
                )
                return
        
        # Verify email_hash matches
        if otp_record.get("email_hash") != email_hash:
            await self.db.increment_otp_attempts(interaction.user.id, code_hash)
            await interaction.followup.send(
                "❌ Email address doesn't match the code. Please use the email you used with `/link_email`.",
                ephemeral=True
            )
            return
        
        # Check attempts
        if otp_record["attempts"] >= self.config.OTP_MAX_ATTEMPTS:
            await interaction.followup.send(
                "❌ Too many failed attempts. Please request a new code with `/link_email`.",
                ephemeral=True
            )
            await self.db.delete_otp(interaction.user.id)
            return
        
        # Check expiry
        expires_at = datetime.fromisoformat(otp_record["expires_at"])
        if datetime.now(timezone.utc) > expires_at:
            await interaction.followup.send(
                "❌ Code has expired. Please request a new code with `/link_email`.",
                ephemeral=True
            )
            await self.db.delete_otp(interaction.user.id)
            return
        
        # Code is valid! Link email
        await self.db.create_user(interaction.user.id)  # Ensure user exists
        await self.db.link_email(interaction.user.id, email_hash)
        await self.db.delete_otp(interaction.user.id)
        
        # Check if email is in paid list
        is_paid = await self.db.is_email_paid(email_hash)
        
        guild = interaction.guild
        if guild and is_paid:
            # Auto-grant Premium
            premium_role = await self.get_role(guild, "premium")
            if premium_role:
                member = guild.get_member(interaction.user.id)
                if member:
                    try:
                        await member.add_roles(premium_role, reason="Email linked and verified, email in paid list")
                        logger.info(f"Auto-granted premium to {member} via email linking")
                        
                        # Log to bot-logs
                        log_channel = await self.get_channel(guild, "bot_logs")
                        if log_channel:
                            embed = discord.Embed(
                                title="✅ Premium Auto-Granted",
                                description=(
                                    f"**User:** {member.mention} ({member})\n"
                                    f"**Reason:** Email linked and verified, email in paid subscriber list"
                                ),
                                color=discord.Color.green(),
                                timestamp=datetime.now(timezone.utc)
                            )
                            try:
                                await log_channel.send(embed=embed)
                            except Exception:
                                pass
                    except Exception as e:
                        logger.error(f"Failed to auto-grant premium: {e}")
        
        if is_paid:
            await interaction.followup.send(
                "✅ Email verified and linked! Your email is in our paid subscriber list, so Premium access has been granted automatically.",
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                "✅ Email verified and linked! If your email is in our paid subscriber list, Premium access will be granted automatically after the next import.",
                ephemeral=True
            )
    
