"""Access Panel cog: unified access control interface."""
import logging
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands

from services.hashing import hash_email, normalize_email, generate_otp_code, hash_otp_code
from services.email_service import EmailService

logger = logging.getLogger(__name__)


class FreeAccessButton(discord.ui.Button):
    """Free Access button with persistent custom_id."""
    
    def __init__(self):
        super().__init__(
            label="✅ Free Access",
            style=discord.ButtonStyle.success,
            custom_id="access_panel:free"
        )
    
    async def callback(self, interaction: discord.Interaction):
        """Handle Free Access button click."""
        # Enforce channel restriction
        if not interaction.guild:
            await interaction.response.send_message("❌ This can only be used in a server.", ephemeral=True)
            return
        
        verify_channel_id = await self.view.get_verify_channel_id(interaction.guild)
        if verify_channel_id and interaction.channel_id != verify_channel_id:
            await interaction.response.send_message(
                f"❌ This panel can only be used in <#{verify_channel_id}>.",
                ephemeral=True
            )
            return
        
        await interaction.response.defer(ephemeral=True)
        
        # Get free role
        onboarding_cog = self.view.bot.get_cog("OnboardingCog")
        if not onboarding_cog:
            await interaction.followup.send("❌ Bot configuration error.", ephemeral=True)
            return
        
        free_role = await onboarding_cog.get_role(interaction.guild, "free")
        if not free_role:
            await interaction.followup.send("❌ Free Member role not found.", ephemeral=True)
            return
        
        member = interaction.guild.get_member(interaction.user.id)
        if not member:
            await interaction.followup.send("❌ Could not find your member information.", ephemeral=True)
            return
        
        if free_role in member.roles:
            await interaction.followup.send("✅ You already have Free Member access!", ephemeral=True)
            return
        
        try:
            await member.add_roles(free_role, reason="Access Panel: Free Access button")
            await interaction.followup.send("✅ You've been granted Free Member access!", ephemeral=True)
            logger.info(f"Assigned free role to {member} via access panel")
        except discord.Forbidden:
            await interaction.followup.send("❌ I don't have permission to assign roles.", ephemeral=True)
        except Exception as e:
            logger.error(f"Error assigning free role: {e}", exc_info=True)
            await interaction.followup.send("❌ An error occurred. Please contact an admin.", ephemeral=True)


class PremiumEmailModal(discord.ui.Modal, title="Premium Access - Email Verification"):
    """Modal for email entry."""
    
    email = discord.ui.TextInput(
        label="Substack Email",
        placeholder="your.email@example.com",
        required=True,
        max_length=255
    )
    
    def __init__(self, view):
        super().__init__()
        self.view = view
    
    async def on_submit(self, interaction: discord.Interaction):
        """Handle email submission."""
        await interaction.response.defer(ephemeral=True)
        
        # Enforce channel restriction
        if not interaction.guild:
            await interaction.followup.send("❌ This can only be used in a server.", ephemeral=True)
            return
        
        verify_channel_id = await self.view.get_verify_channel_id(interaction.guild)
        if verify_channel_id and interaction.channel_id != verify_channel_id:
            await interaction.followup.send(
                f"❌ This panel can only be used in <#{verify_channel_id}>.",
                ephemeral=True
            )
            return
        
        email_linking_cog = self.view.bot.get_cog("EmailLinkingCog")
        if not email_linking_cog:
            await interaction.followup.send("❌ Email service not available.", ephemeral=True)
            return
        
        # Normalize and hash email
        normalized = normalize_email(self.email.value)
        email_hash = hash_email(normalized)
        
        # Check if email is already linked to another user
        existing_user = await self.view.bot.db.get_user_by_email_hash(email_hash)
        if existing_user and existing_user["discord_user_id"] != interaction.user.id:
            await interaction.followup.send("❌ This email is already linked to another Discord account.", ephemeral=True)
            return
        
        # Generate OTP
        otp_code, otp_hash = generate_otp_code()
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=self.view.bot.config.OTP_EXPIRY_MINUTES)
        
        # Store OTP with email hash
        await self.view.bot.db.store_otp(interaction.user.id, otp_hash, email_hash, expires_at)
        
        # Send email
        email_service = EmailService(self.view.bot.config.SENDGRID_API_KEY, self.view.bot.config.FROM_EMAIL)
        if not email_service.enabled:
            await interaction.followup.send("❌ Email service is not configured.", ephemeral=True)
            return
        
        email_sent = await email_service.send_otp(normalized, otp_code)
        
        if not email_sent:
            await interaction.followup.send("❌ Failed to send verification email. Please try again later.", ephemeral=True)
            return
        
        # Show "Enter Code" button
        view = EnterCodeView(self.view.bot, email_hash)
        await interaction.followup.send(
            f"✅ Verification code sent to your email! Click the button below to enter your code.\n\n"
            f"**Note:** The code expires in {self.view.bot.config.OTP_EXPIRY_MINUTES} minutes.",
            view=view,
            ephemeral=True
        )


class EnterCodeModal(discord.ui.Modal, title="Enter Verification Code"):
    """Modal for OTP code entry."""
    
    code = discord.ui.TextInput(
        label="6-Digit Code",
        placeholder="123456",
        required=True,
        max_length=6,
        min_length=6
    )
    
    def __init__(self, bot, email_hash: str):
        super().__init__()
        self.bot = bot
        self.email_hash = email_hash
    
    async def on_submit(self, interaction: discord.Interaction):
        """Handle code submission."""
        await interaction.response.defer(ephemeral=True)
        
        # Validate code format
        code = self.code.value.strip()
        if not code.isdigit() or len(code) != 6:
            await interaction.followup.send("❌ Invalid code format. Please enter a 6-digit number.", ephemeral=True)
            return
        
        # Hash code
        code_hash = hash_otp_code(code)
        
        # Get OTP record
        otp_record = await self.bot.db.get_otp(interaction.user.id, code_hash)
        if not otp_record:
            otp_record = await self.bot.db.get_otp_by_email_hash(interaction.user.id, self.email_hash)
            if not otp_record:
                await self.bot.db.increment_otp_attempts(interaction.user.id, code_hash)
                await interaction.followup.send("❌ Invalid or expired code. Please request a new code.", ephemeral=True)
                return
        
        # Verify email_hash matches
        if otp_record.get("email_hash") != self.email_hash:
            await self.bot.db.increment_otp_attempts(interaction.user.id, code_hash)
            await interaction.followup.send("❌ Email address doesn't match the code.", ephemeral=True)
            return
        
        # Check attempts
        if otp_record["attempts"] >= self.bot.config.OTP_MAX_ATTEMPTS:
            await interaction.followup.send("❌ Too many failed attempts. Please request a new code.", ephemeral=True)
            await self.bot.db.delete_otp(interaction.user.id)
            return
        
        # Check expiry
        expires_at = datetime.fromisoformat(otp_record["expires_at"])
        if datetime.now(timezone.utc) > expires_at:
            await interaction.followup.send("❌ Code has expired. Please request a new code.", ephemeral=True)
            await self.bot.db.delete_otp(interaction.user.id)
            return
        
        # Code is valid! Link email
        await self.bot.db.create_user(interaction.user.id)
        await self.bot.db.link_email(interaction.user.id, self.email_hash)
        await self.bot.db.delete_otp(interaction.user.id)
        
        # Check if email is in paid list
        is_paid = await self.bot.db.is_email_paid(self.email_hash)
        
        guild = interaction.guild
        if guild and is_paid:
            # Auto-grant Premium
            onboarding_cog = self.bot.get_cog("OnboardingCog")
            if onboarding_cog:
                premium_role = await onboarding_cog.get_role(guild, "premium")
                if premium_role:
                    member = guild.get_member(interaction.user.id)
                    if member:
                        try:
                            await member.add_roles(premium_role, reason="Email verified, email in paid subscriber list")
                            logger.info(f"Auto-granted premium to {member} via access panel email")
                            
                            # Log to bot-logs
                            admin_cog = self.bot.get_cog("AdminRolesCog")
                            if admin_cog:
                                log_channel = await admin_cog.get_channel(guild, "bot_logs")
                                if log_channel:
                                    embed = discord.Embed(
                                        title="✅ Premium Auto-Granted",
                                        description=(
                                            f"**User:** {member.mention} ({member})\n"
                                            f"**Method:** Access Panel - Email Verification\n"
                                            f"**Reason:** Email verified, email in paid subscriber list"
                                        ),
                                        color=discord.Color.green(),
                                        timestamp=datetime.now(timezone.utc)
                                    )
                                    try:
                                        await log_channel.send(embed=embed)
                                    except Exception:
                                        pass
                            
                            await interaction.followup.send(
                                "✅ **Premium access granted!** Your email is in our paid subscriber list, so you've been automatically approved.",
                                ephemeral=True
                            )
                            return
                        except Exception as e:
                            logger.error(f"Failed to auto-grant premium: {e}")
        
        await interaction.followup.send(
            "✅ Email verified and linked! If your email is in our paid subscriber list, Premium access will be granted automatically after the next import.",
            ephemeral=True
        )


class EnterCodeView(discord.ui.View):
    """View with Enter Code button."""
    
    def __init__(self, bot, email_hash: str):
        super().__init__(timeout=600)  # 10 minutes timeout
        self.bot = bot
        self.email_hash = email_hash
    
    @discord.ui.button(label="Enter Code", style=discord.ButtonStyle.primary)
    async def enter_code(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Open code entry modal."""
        modal = EnterCodeModal(self.bot, self.email_hash)
        await interaction.response.send_modal(modal)


class PremiumEmailButton(discord.ui.Button):
    """Premium Access (Email) button."""
    
    def __init__(self):
        super().__init__(
            label="📧 Premium Access (Email)",
            style=discord.ButtonStyle.primary,
            custom_id="access_panel:premium_email"
        )
    
    async def callback(self, interaction: discord.Interaction):
        """Handle Premium Email button click."""
        # Enforce channel restriction
        if not interaction.guild:
            await interaction.response.send_message("❌ This can only be used in a server.", ephemeral=True)
            return
        
        verify_channel_id = await self.view.get_verify_channel_id(interaction.guild)
        if verify_channel_id and interaction.channel_id != verify_channel_id:
            await interaction.response.send_message(
                f"❌ This panel can only be used in <#{verify_channel_id}>.",
                ephemeral=True
            )
            return
        
        # Show email modal
        modal = PremiumEmailModal(self.view)
        await interaction.response.send_modal(modal)


class PremiumScreenshotModal(discord.ui.Modal, title="Premium Access - Screenshot Verification"):
    """Modal for screenshot verification."""
    
    email = discord.ui.TextInput(
        label="Substack Email (Optional)",
        placeholder="your.email@example.com",
        required=False,
        max_length=255
    )
    
    notes = discord.ui.TextInput(
        label="Additional Notes (Optional)",
        placeholder="Any additional information...",
        required=False,
        style=discord.TextStyle.paragraph,
        max_length=500
    )
    
    def __init__(self, view):
        super().__init__()
        self.view = view
    
    async def on_submit(self, interaction: discord.Interaction):
        """Handle modal submission."""
        await interaction.response.defer(ephemeral=True)
        
        # Enforce channel restriction
        if not interaction.guild:
            await interaction.followup.send("❌ This can only be used in a server.", ephemeral=True)
            return
        
        verify_channel_id = await self.view.get_verify_channel_id(interaction.guild)
        if verify_channel_id and interaction.channel_id != verify_channel_id:
            await interaction.followup.send(
                f"❌ This panel can only be used in <#{verify_channel_id}>.",
                ephemeral=True
            )
            return
        
        # Store email hash if provided
        claimed_email_hash = None
        if self.email.value:
            normalized = normalize_email(self.email.value)
            claimed_email_hash = hash_email(normalized)
        
        # Store pending request info temporarily
        verification_cog = self.view.bot.get_cog("VerificationQueueCog")
        if verification_cog:
            verification_cog.pending_requests[interaction.user.id] = {
                "email_hash": claimed_email_hash,
                "notes": self.notes.value if self.notes.value else None
            }
        
        # Show submit screenshot button
        view = SubmitScreenshotView(self.view.bot, claimed_email_hash)
        await interaction.followup.send(
            f"✅ Request received! Please attach your proof image in your next message in this channel "
            f"within {self.view.bot.config.PROOF_TIMEOUT_MINUTES} minutes, then click the button below.",
            view=view,
            ephemeral=True
        )


class SubmitScreenshotButton(discord.ui.Button):
    """Submit Screenshot button."""
    
    def __init__(self, bot, claimed_email_hash: str = None):
        super().__init__(label="Submit Screenshot", style=discord.ButtonStyle.primary)
        self.bot = bot
        self.claimed_email_hash = claimed_email_hash
    
    async def callback(self, interaction: discord.Interaction):
        """Submit screenshot proof."""
        await interaction.response.defer(ephemeral=True)
        
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("❌ This can only be used in a server.", ephemeral=True)
            return
        
        # Enforce channel restriction
        access_panel_cog = self.bot.get_cog("AccessPanelCog")
        if access_panel_cog:
            verify_channel = await access_panel_cog.get_channel(guild, "verify")
            if verify_channel and interaction.channel_id != verify_channel.id:
                await interaction.followup.send(
                    f"❌ This can only be used in {verify_channel.mention}.",
                    ephemeral=True
                )
                return
            if verify_channel:
                verify_channel_obj = verify_channel
            else:
                verify_channel_obj = interaction.channel
        else:
            verify_channel_obj = interaction.channel
        
        if not verify_channel_obj:
            await interaction.followup.send("❌ Channel not found.", ephemeral=True)
            return
        
        # Check for pending request
        pending = await self.bot.db.get_pending_verify_request(interaction.user.id)
        if pending:
            await interaction.followup.send("❌ You already have a pending verification request.", ephemeral=True)
            return
        
        # Look for recent messages with attachments
        timeout_minutes = self.bot.config.PROOF_TIMEOUT_MINUTES
        cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)
        
        attachment_urls = []
        async for message in verify_channel_obj.history(limit=20, after=cutoff_time):
            if message.author.id == interaction.user.id and message.attachments:
                for attachment in message.attachments:
                    if attachment.content_type and attachment.content_type.startswith("image/"):
                        attachment_urls.append(attachment.url)
        
        if not attachment_urls:
            await interaction.followup.send(
                f"❌ No image attachments found in your recent messages. "
                f"Please upload an image in {verify_channel_obj.mention} and try again.",
                ephemeral=True
            )
            return
        
        # Get email hash from pending request
        verification_cog = self.bot.get_cog("VerificationQueueCog")
        claimed_email_hash = self.claimed_email_hash
        if verification_cog:
            pending_info = verification_cog.pending_requests.pop(interaction.user.id, {})
            if not claimed_email_hash:
                claimed_email_hash = pending_info.get("email_hash")
        
        # Create verification request
        request_id = await self.bot.db.create_verify_request(
            interaction.user.id,
            claimed_email_hash,
            attachment_urls
        )
        
        # Record rate limit
        if verification_cog:
            verification_cog.rate_limiter.record_action(interaction.user.id, "verify_premium")
        
        # Post to verify queue
        admin_cog = self.bot.get_cog("AdminRolesCog")
        if admin_cog:
            queue_channel = await admin_cog.get_channel(guild, "verify_queue")
            if queue_channel:
                from cogs.verification_queue import VerifyQueueButtons
                
                embed = discord.Embed(
                    title="💎 New Premium Verification Request",
                    description=f"**User:** {interaction.user.mention} ({interaction.user})\n**User ID:** {interaction.user.id}",
                    color=discord.Color.blue(),
                    timestamp=datetime.now(timezone.utc)
                )
                
                if claimed_email_hash:
                    is_paid = await self.bot.db.is_email_paid(claimed_email_hash)
                    email_status = "✅ In paid list" if is_paid else "❌ Not in paid list"
                    embed.add_field(name="Email Status", value=email_status, inline=True)
                
                if attachment_urls:
                    embed.add_field(
                        name="Proof Attachments",
                        value="\n".join([f"[Image {i+1}]({url})" for i, url in enumerate(attachment_urls[:5])]),
                        inline=False
                    )
                
                embed.set_footer(text=f"Request ID: {request_id}")
                
                view = VerifyQueueButtons(self.bot, request_id)
                await queue_channel.send(embed=embed, view=view)
        
        await interaction.followup.send(
            "✅ Your verification request has been submitted! A moderator will review it shortly.",
            ephemeral=True
        )


class SubmitScreenshotButton(discord.ui.Button):
    """Submit Screenshot button."""
    
    def __init__(self, bot, claimed_email_hash: str = None):
        super().__init__(label="Submit Screenshot", style=discord.ButtonStyle.primary)
        self.bot = bot
        self.claimed_email_hash = claimed_email_hash
    
    async def callback(self, interaction: discord.Interaction):
        """Submit screenshot proof."""
        await interaction.response.defer(ephemeral=True)
        
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("❌ This can only be used in a server.", ephemeral=True)
            return
        
        # Enforce channel restriction
        access_panel_cog = self.bot.get_cog("AccessPanelCog")
        if access_panel_cog:
            verify_channel = await access_panel_cog.get_channel(guild, "verify")
            if verify_channel and interaction.channel_id != verify_channel.id:
                await interaction.followup.send(
                    f"❌ This can only be used in {verify_channel.mention}.",
                    ephemeral=True
                )
                return
            if verify_channel:
                verify_channel_obj = verify_channel
            else:
                verify_channel_obj = interaction.channel
        else:
            verify_channel_obj = interaction.channel
        
        if not verify_channel_obj:
            await interaction.followup.send("❌ Channel not found.", ephemeral=True)
            return
        
        # Check for pending request
        pending = await self.bot.db.get_pending_verify_request(interaction.user.id)
        if pending:
            await interaction.followup.send("❌ You already have a pending verification request.", ephemeral=True)
            return
        
        # Look for recent messages with attachments
        timeout_minutes = self.bot.config.PROOF_TIMEOUT_MINUTES
        cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)
        
        attachment_urls = []
        async for message in verify_channel_obj.history(limit=20, after=cutoff_time):
            if message.author.id == interaction.user.id and message.attachments:
                for attachment in message.attachments:
                    if attachment.content_type and attachment.content_type.startswith("image/"):
                        attachment_urls.append(attachment.url)
        
        if not attachment_urls:
            await interaction.followup.send(
                f"❌ No image attachments found in your recent messages. "
                f"Please upload an image in {verify_channel_obj.mention} and try again.",
                ephemeral=True
            )
            return
        
        # Get email hash from pending request
        verification_cog = self.bot.get_cog("VerificationQueueCog")
        claimed_email_hash = self.claimed_email_hash
        if verification_cog:
            pending_info = verification_cog.pending_requests.pop(interaction.user.id, {})
            if not claimed_email_hash:
                claimed_email_hash = pending_info.get("email_hash")
        
        # Create verification request
        request_id = await self.bot.db.create_verify_request(
            interaction.user.id,
            claimed_email_hash,
            attachment_urls
        )
        
        # Record rate limit
        if verification_cog:
            verification_cog.rate_limiter.record_action(interaction.user.id, "verify_premium")
        
        # Post to verify queue
        admin_cog = self.bot.get_cog("AdminRolesCog")
        if admin_cog:
            queue_channel = await admin_cog.get_channel(guild, "verify_queue")
            if queue_channel:
                from cogs.verification_queue import VerifyQueueButtons
                
                embed = discord.Embed(
                    title="💎 New Premium Verification Request",
                    description=f"**User:** {interaction.user.mention} ({interaction.user})\n**User ID:** {interaction.user.id}",
                    color=discord.Color.blue(),
                    timestamp=datetime.now(timezone.utc)
                )
                
                if claimed_email_hash:
                    is_paid = await self.bot.db.is_email_paid(claimed_email_hash)
                    email_status = "✅ In paid list" if is_paid else "❌ Not in paid list"
                    embed.add_field(name="Email Status", value=email_status, inline=True)
                
                if attachment_urls:
                    embed.add_field(
                        name="Proof Attachments",
                        value="\n".join([f"[Image {i+1}]({url})" for i, url in enumerate(attachment_urls[:5])]),
                        inline=False
                    )
                
                embed.set_footer(text=f"Request ID: {request_id}")
                
                view = VerifyQueueButtons(self.bot, request_id)
                await queue_channel.send(embed=embed, view=view)
        
        await interaction.followup.send(
            "✅ Your verification request has been submitted! A moderator will review it shortly.",
            ephemeral=True
        )


class SubmitScreenshotView(discord.ui.View):
    """View with Submit Screenshot button."""
    
    def __init__(self, bot, claimed_email_hash: str = None):
        super().__init__(timeout=600)  # 10 minutes timeout
        self.bot = bot
        self.claimed_email_hash = claimed_email_hash
        self.add_item(SubmitScreenshotButton(bot, claimed_email_hash))


class PremiumScreenshotButton(discord.ui.Button):
    """Premium Access (Screenshot) button."""
    
    def __init__(self):
        super().__init__(
            label="🖼️ Premium Access (Screenshot)",
            style=discord.ButtonStyle.secondary,
            custom_id="access_panel:premium_screenshot"
        )
    
    async def callback(self, interaction: discord.Interaction):
        """Handle Premium Screenshot button click."""
        # Enforce channel restriction
        if not interaction.guild:
            await interaction.response.send_message("❌ This can only be used in a server.", ephemeral=True)
            return
        
        verify_channel_id = await self.view.get_verify_channel_id(interaction.guild)
        if verify_channel_id and interaction.channel_id != verify_channel_id:
            await interaction.response.send_message(
                f"❌ This panel can only be used in <#{verify_channel_id}>.",
                ephemeral=True
            )
            return
        
        # Show screenshot modal
        modal = PremiumScreenshotModal(self.view)
        await interaction.response.send_modal(modal)


class AccessPanelView(discord.ui.View):
    """Persistent view for Access Panel."""
    
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot
        self.add_item(FreeAccessButton())
        self.add_item(PremiumEmailButton())
        self.add_item(PremiumScreenshotButton())
    
    async def get_verify_channel_id(self, guild: discord.Guild) -> int | None:
        """Get verify channel ID."""
        onboarding_cog = self.bot.get_cog("OnboardingCog")
        if onboarding_cog:
            verify_channel = await onboarding_cog.get_channel(guild, "verify")
            if verify_channel:
                return verify_channel.id
        return None


class AccessPanelCog(commands.Cog):
    """Access Panel functionality."""
    
    def __init__(self, bot):
        self.bot = bot
        self.config = bot.config
        self.db = bot.db
    
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
    
    async def check_admin_permissions(self, interaction: discord.Interaction) -> bool:
        """Check if user has admin permissions."""
        if not interaction.guild:
            return False
        
        member = interaction.user
        guild = interaction.guild
        
        admin_cog = self.bot.get_cog("AdminRolesCog")
        if admin_cog:
            admin_role = await admin_cog.get_role(guild, "admin")
            if admin_role and admin_role in member.roles:
                return True
        
        if member.guild_permissions.administrator:
            return True
        
        return False
    
    @app_commands.command(name="post_access_panel", description="Post or update the Access Panel in #verify (Admin only)")
    async def post_access_panel(self, interaction: discord.Interaction):
        """Post or update Access Panel."""
        if not await self.check_admin_permissions(interaction):
            await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("❌ This command can only be used in a server.", ephemeral=True)
            return
        
        verify_channel = await self.get_channel(guild, "verify")
        if not verify_channel:
            await interaction.followup.send("❌ Verify channel not found.", ephemeral=True)
            return
        
        # Check for existing panel
        existing_panel_id = await self.db.get_access_panel_message_id(guild.id)
        
        embed = discord.Embed(
            title="💎 Hidden Gems Research – The Gem Vault",
            description=(
                "**Welcome! Choose your access level:**\n\n"
                "**✅ Free Access**\n"
                "Get Free Member access instantly.\n\n"
                "**📧 Premium Access (Email)**\n"
                "Link your Substack email for automatic verification.\n"
                "If your email is in our paid subscriber list, Premium will be granted immediately.\n\n"
                "**🖼️ Premium Access (Screenshot)**\n"
                "Submit proof of your Substack subscription for manual review.\n\n"
                "**⚠️ Important Disclaimer**\n"
                "This bot and community do not provide financial advice. "
                "All trading decisions are your own responsibility. "
                "The bot only automates server administration tasks."
            ),
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )
        
        view = AccessPanelView(self.bot)
        
        try:
            if existing_panel_id:
                # Try to edit existing message
                try:
                    existing_message = await verify_channel.fetch_message(existing_panel_id)
                    await existing_message.edit(embed=embed, view=view)
                    await self.db.update_access_panel_message_id(guild.id, existing_message.id)
                    await interaction.followup.send(f"✅ Access Panel updated in {verify_channel.mention}!", ephemeral=True)
                    return
                except discord.NotFound:
                    # Message was deleted, create new one
                    pass
                except Exception as e:
                    logger.warning(f"Failed to edit existing panel: {e}")
            
            # Post new panel
            message = await verify_channel.send(embed=embed, view=view)
            await self.db.set_access_panel_message_id(guild.id, message.id)
            await interaction.followup.send(f"✅ Access Panel posted in {verify_channel.mention}!", ephemeral=True)
            
        except Exception as e:
            logger.error(f"Error posting access panel: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error posting panel: {str(e)}", ephemeral=True)
