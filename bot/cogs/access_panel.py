"""Access Panel cog: unified access control interface."""
import logging
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands

from services.hashing import hash_email, normalize_email, generate_otp_code, hash_otp_code
from services.email_service import EmailService

logger = logging.getLogger(__name__)

def _redact_hash(h: str | None) -> str:
    if not h:
        return "None"
    if len(h) <= 12:
        return h
    return f"{h[:8]}…{h[-6:]}"

def _as_utc_datetime(value) -> datetime | None:
    """Parse DB timestamps from either datetime or isoformat strings."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None
    return None

async def _log_to_bot_logs(bot, guild: discord.Guild, title: str, description: str, color: discord.Color) -> None:
    """Best-effort log to #bot-logs for Access Panel flows."""
    try:
        access_panel_cog = bot.get_cog("AccessPanelCog")
        if not access_panel_cog:
            return
        log_channel = await access_panel_cog.get_channel(guild, "bot_logs")
        if not log_channel:
            return
        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        await log_channel.send(embed=embed)
    except Exception:
        pass

async def _safe_ephemeral_send(interaction: discord.Interaction, content: str) -> None:
    """Send ephemeral; if forbidden, DM user instead."""
    try:
        await interaction.followup.send(content, ephemeral=True)
        return
    except discord.Forbidden:
        try:
            await interaction.user.send(content)
        except Exception:
            pass
    except Exception:
        # If followup fails for any reason, last resort DM
        try:
            await interaction.user.send(content)
        except Exception:
            pass

def _attachment_is_image(attachment: discord.Attachment) -> bool:
    """Return True if the attachment is an image (best-effort)."""
    try:
        if attachment.content_type and attachment.content_type.startswith("image/"):
            return True
    except Exception:
        pass
    name = (getattr(attachment, "filename", "") or "").lower()
    return name.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))


class UploadScreenshotDMButton(discord.ui.Button):
    """Collect proof via DM so screenshots are never public."""

    def __init__(self, bot):
        super().__init__(label="Upload Screenshot (DM)", style=discord.ButtonStyle.primary)
        self.bot = bot

    async def callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.InteractionResponded:
            pass

        if not interaction.guild:
            await interaction.followup.send("❌ This can only be used in a server.", ephemeral=True)
            return

        # Block if user already has a pending verification request
        pending = await self.bot.db.get_pending_verify_request(interaction.user.id)
        if pending:
            await interaction.followup.send("❌ You already have a pending verification request.", ephemeral=True)
            return

        # Pull pending metadata saved from the modal
        verification_cog = self.bot.get_cog("VerificationQueueCog")
        pending_info = {}
        if verification_cog:
            pending_info = verification_cog.pending_requests.pop(interaction.user.id, {})
        claimed_email_hash = pending_info.get("email_hash")

        # DM upload prompt
        try:
            dm = await interaction.user.create_dm()
            await dm.send(
                "🖼️ **Premium Access (Screenshot) – Upload**\n\n"
                "Please reply to this DM with your Substack subscription screenshot attached.\n\n"
                "**Important:** Do **not** post screenshots in the public `#verify` channel.\n"
                "**Your screenshot will only be visible to moderators.**\n\n"
                f"⏳ You have **{self.bot.config.PROOF_TIMEOUT_MINUTES} minutes** to upload."
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I couldn't DM you. Please enable DMs from this server and try again (or contact a moderator).",
                ephemeral=True
            )
            # Put pending info back so they can retry
            if verification_cog:
                verification_cog.pending_requests[interaction.user.id] = pending_info
            return

        await interaction.followup.send(
            "✅ Check your DMs for an upload prompt.\n\n**Your screenshot will only be visible to moderators.**",
            ephemeral=True
        )

        timeout_seconds = int(self.bot.config.PROOF_TIMEOUT_MINUTES * 60)

        def check(msg: discord.Message) -> bool:
            if msg.author.id != interaction.user.id:
                return False
            if msg.channel.id != dm.id:
                return False
            if not msg.attachments:
                return False
            return any(_attachment_is_image(a) for a in msg.attachments)

        try:
            proof_msg: discord.Message = await self.bot.wait_for("message", timeout=timeout_seconds, check=check)
        except Exception:
            try:
                await dm.send(
                    "⏳ Upload timed out. Please go back to `#verify`, click **Premium Access (Screenshot)** again, and retry.\n\n"
                    "**Your screenshot will only be visible to moderators.**"
                )
            except Exception:
                pass
            return

        attachment_urls = [a.url for a in proof_msg.attachments if _attachment_is_image(a)]
        if not attachment_urls:
            try:
                await dm.send("❌ I didn't detect an image attachment. Please try again.")
            except Exception:
                pass
            return

        # Create verification request (URL-only; no local storage)
        try:
            request_id = await self.bot.db.create_verify_request(
                interaction.user.id,
                claimed_email_hash,
                attachment_urls
            )
        except Exception as e:
            logger.error(f"Error creating verify request (DM flow): {e}", exc_info=True)
            try:
                await dm.send("❌ Error creating your verification request. Please try again later or contact a moderator.")
            except Exception:
                pass
            return

        # Record rate limit
        if verification_cog:
            verification_cog.rate_limiter.record_action(interaction.user.id, "verify_premium")

        # Post to verify queue (mods/admins only)
        admin_cog = self.bot.get_cog("AdminRolesCog")
        queue_channel = await admin_cog.get_channel(interaction.guild, "verify_queue") if admin_cog else None

        if not queue_channel:
            logger.warning("verify_queue channel not found (DM flow)")
            try:
                await dm.send(
                    "⚠️ Your verification request was created, but the moderator queue channel wasn't found. "
                    "Please contact a moderator."
                )
            except Exception:
                pass
            return

        try:
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

            embed.add_field(
                name="Proof Attachments",
                value="\n".join([f"[Image {i+1}]({url})" for i, url in enumerate(attachment_urls[:5])]),
                inline=False
            )
            embed.set_image(url=attachment_urls[0])
            embed.set_footer(text=f"Request ID: {request_id}")

            view = VerifyQueueButtons(self.bot, request_id)
            await queue_channel.send(embed=embed, view=view)
            logger.info(f"Posted verification request {request_id} to verify-queue (DM flow) for user {interaction.user.id}")
        except Exception as e:
            logger.error(f"Error posting to verify queue (DM flow): {e}", exc_info=True)
            try:
                await dm.send(
                    "⚠️ Your verification request was created, but I couldn't post it to the moderator queue. "
                    "Please contact a moderator."
                )
            except Exception:
                pass
            return

        try:
            await dm.send(
                "✅ Your verification request has been submitted! A moderator will review it shortly.\n\n"
                "**Your screenshot will only be visible to moderators.**"
            )
        except Exception:
            pass


class UploadScreenshotDMView(discord.ui.View):
    """Ephemeral view that prompts DM-based upload."""

    def __init__(self, bot):
        super().__init__(timeout=600)
        self.add_item(UploadScreenshotDMButton(bot))


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
        
        # Get verify channel ID
        access_panel_cog = interaction.client.get_cog("AccessPanelCog")
        verify_channel_id = None
        if access_panel_cog:
            verify_channel = await access_panel_cog.get_channel(interaction.guild, "verify")
            if verify_channel:
                verify_channel_id = verify_channel.id
        
        if verify_channel_id and interaction.channel_id != verify_channel_id:
            await interaction.response.send_message(
                f"❌ This panel can only be used in <#{verify_channel_id}>.",
                ephemeral=True
            )
            return
        
        await interaction.response.defer(ephemeral=True)
        
        # Get free role
        onboarding_cog = interaction.client.get_cog("OnboardingCog")
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
        bot = interaction.client
        guild = interaction.guild

        try:
            # Enforce channel restriction
            if not guild:
                await _safe_ephemeral_send(interaction, "❌ This can only be used in a server.")
                return

            verify_channel_id = await self.view.get_verify_channel_id(guild)
            if verify_channel_id and interaction.channel_id != verify_channel_id:
                await _safe_ephemeral_send(interaction, f"❌ This panel can only be used in <#{verify_channel_id}>.")
                return

            # Normalize and hash email
            normalized = normalize_email(self.email.value)
            email_hash = hash_email(normalized)

            await _log_to_bot_logs(
                bot,
                guild,
                title="📩 Access Panel: OTP Link Requested",
                description=(
                    f"**User:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                    f"**Email Hash:** `{_redact_hash(email_hash)}`"
                ),
                color=discord.Color.blurple(),
            )

            # Check if email is already linked to another user
            existing_user = await bot.db.get_user_by_email_hash(email_hash)
            if existing_user and existing_user["discord_user_id"] != interaction.user.id:
                await _log_to_bot_logs(
                    bot,
                    guild,
                    title="❌ Access Panel: OTP Link Blocked",
                    description=(
                        f"**User:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                        f"**Email Hash:** `{_redact_hash(email_hash)}`\n"
                        f"**Reason:** Email already linked to `{existing_user['discord_user_id']}`"
                    ),
                    color=discord.Color.red(),
                )
                await _safe_ephemeral_send(interaction, "❌ This email is already linked to another Discord account.")
                return

            # Generate OTP
            otp_code, otp_hash = generate_otp_code()
            expires_at = datetime.now(timezone.utc) + timedelta(minutes=bot.config.OTP_EXPIRY_MINUTES)

            # Store OTP with email hash
            await bot.db.delete_otps_for_user_email(interaction.user.id, email_hash)
            await bot.db.store_otp(interaction.user.id, otp_hash, email_hash, expires_at)

            # Send email
            email_service = EmailService(bot.config.SENDGRID_API_KEY, bot.config.FROM_EMAIL)
            if not email_service.enabled:
                await _log_to_bot_logs(
                    bot,
                    guild,
                    title="❌ Access Panel: Email Service Not Configured",
                    description=f"**User:** {interaction.user.mention} (`{interaction.user.id}`)",
                    color=discord.Color.red(),
                )
                await _safe_ephemeral_send(interaction, "❌ Email service is not configured. Please contact an administrator.")
                return

            email_sent = await email_service.send_otp(normalized, otp_code)
            if not email_sent:
                await _log_to_bot_logs(
                    bot,
                    guild,
                    title="❌ Access Panel: OTP Email Failed",
                    description=(
                        f"**User:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                        f"**Email Hash:** `{_redact_hash(email_hash)}`"
                    ),
                    color=discord.Color.red(),
                )
                await _safe_ephemeral_send(interaction, "❌ Failed to send verification email. Please try again later.")
                return

            await _log_to_bot_logs(
                bot,
                guild,
                title="✅ Access Panel: OTP Email Sent",
                description=(
                    f"**User:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                    f"**Email Hash:** `{_redact_hash(email_hash)}`\n"
                    f"**Expires At:** `{expires_at.isoformat()}`"
                ),
                color=discord.Color.green(),
            )

            # Show "Enter Code" button
            view = EnterCodeView(bot, email_hash)
            await interaction.followup.send(
                "✅ Verification code sent to your email! Click the button below to enter your code.\n\n"
                f"**Note:** The code expires in {bot.config.OTP_EXPIRY_MINUTES} minutes.",
                view=view,
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"Error in Access Panel PremiumEmailModal: {e}", exc_info=True)
            if guild:
                await _log_to_bot_logs(
                    bot,
                    guild,
                    title="❌ Access Panel: OTP Link Error",
                    description=f"**User:** {interaction.user.mention} (`{interaction.user.id}`)\n**Error:** `{type(e).__name__}: {e}`",
                    color=discord.Color.red(),
                )
            await _safe_ephemeral_send(
                interaction,
                "❌ An unexpected error occurred while sending your code. Please try again or contact an administrator.",
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

        bot = interaction.client
        guild = interaction.guild

        try:
            if not guild:
                await _safe_ephemeral_send(interaction, "❌ This can only be used in a server.")
                return

            await _log_to_bot_logs(
                bot,
                guild,
                title="🔐 Access Panel: OTP Confirm Requested",
                description=(
                    f"**User:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                    f"**Email Hash:** `{_redact_hash(self.email_hash)}`"
                ),
                color=discord.Color.blurple(),
            )

            code = self.code.value.strip()
            if not code.isdigit() or len(code) != 6:
                await _safe_ephemeral_send(interaction, "❌ Invalid code format. Please enter a 6-digit number.")
                return

            otp_record = await self.bot.db.get_otp_by_email_hash(interaction.user.id, self.email_hash)
            if not otp_record:
                await _log_to_bot_logs(
                    bot,
                    guild,
                    title="❌ Access Panel: OTP Confirm Failed",
                    description=(
                        f"**User:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                        f"**Email Hash:** `{_redact_hash(self.email_hash)}`\n"
                        f"**Stage:** Load OTP\n"
                        f"**Result:** No unexpired OTP found"
                    ),
                    color=discord.Color.red(),
                )
                await _safe_ephemeral_send(
                    interaction,
                    "❌ No active code found. Please click **Premium Access (Email)** again to request a new code.",
                )
                return

            stored_code_hash = otp_record.get("code_hash")
            attempts = int(otp_record.get("attempts") or 0)
            expires_at = _as_utc_datetime(otp_record.get("expires_at"))

            await _log_to_bot_logs(
                bot,
                guild,
                title="📦 Access Panel: OTP Loaded",
                description=(
                    f"**User:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                    f"**Email Hash:** `{_redact_hash(self.email_hash)}`\n"
                    f"**Stored Code Hash:** `{_redact_hash(stored_code_hash)}`\n"
                    f"**Attempts:** `{attempts}`\n"
                    f"**Expires At:** `{expires_at.isoformat() if expires_at else 'Unknown'}`"
                ),
                color=discord.Color.blurple(),
            )

            now = datetime.now(timezone.utc)
            if not expires_at or now > expires_at:
                await self.bot.db.delete_otps_for_user_email(interaction.user.id, self.email_hash)
                await _safe_ephemeral_send(interaction, "❌ Code has expired. Please request a new code.")
                return

            if attempts >= self.bot.config.OTP_MAX_ATTEMPTS:
                await self.bot.db.delete_otps_for_user_email(interaction.user.id, self.email_hash)
                await _safe_ephemeral_send(
                    interaction,
                    "❌ Too many failed attempts. Please request a new code by clicking **Premium Access (Email)** again.",
                )
                return

            input_code_hash = hash_otp_code(code)
            if not stored_code_hash or input_code_hash != stored_code_hash:
                if stored_code_hash:
                    await self.bot.db.increment_otp_attempts(interaction.user.id, stored_code_hash)
                    refreshed = await self.bot.db.get_otp(interaction.user.id, stored_code_hash)
                    new_attempts = int((refreshed or {}).get("attempts") or (attempts + 1))
                else:
                    new_attempts = attempts + 1

                remaining = max(self.bot.config.OTP_MAX_ATTEMPTS - new_attempts, 0)
                if new_attempts >= self.bot.config.OTP_MAX_ATTEMPTS:
                    await self.bot.db.delete_otps_for_user_email(interaction.user.id, self.email_hash)
                    await _safe_ephemeral_send(
                        interaction,
                        "❌ Incorrect code. You’ve reached the maximum attempts. Please request a new code.",
                    )
                    return

                await _safe_ephemeral_send(
                    interaction,
                    f"❌ Incorrect code. Please try again. **Attempts remaining:** {remaining}",
                )
                return

            # Valid -> verify user and clear OTP
            await self.bot.db.create_user(interaction.user.id)
            await self.bot.db.link_email(interaction.user.id, self.email_hash)
            await self.bot.db.delete_otps_for_user_email(interaction.user.id, self.email_hash)

            is_paid = await self.bot.db.is_email_paid(self.email_hash)
            if not is_paid:
                await _safe_ephemeral_send(
                    interaction,
                    "✅ Email verified ✅ Premium will be granted after next subscriber sync",
                )
                return

            # Paid -> attempt to grant Premium role
            onboarding_cog = self.bot.get_cog("OnboardingCog")
            if not onboarding_cog:
                await _safe_ephemeral_send(
                    interaction,
                    "✅ Email verified.\n\n⚠️ Your email is paid, but role assignment is unavailable right now. Please contact an administrator.",
                )
                return

            premium_role = await onboarding_cog.get_role(guild, "premium")
            if not premium_role:
                await _safe_ephemeral_send(
                    interaction,
                    "✅ Email verified.\n\n⚠️ Premium role not found. Please contact an administrator.",
                )
                return

            member = guild.get_member(interaction.user.id)
            if not member:
                try:
                    member = await guild.fetch_member(interaction.user.id)
                except Exception:
                    member = None

            if not member:
                await _safe_ephemeral_send(
                    interaction,
                    "✅ Email verified.\n\n⚠️ Could not find your member record to grant Premium. Please contact an administrator.",
                )
                return

            try:
                if premium_role not in member.roles:
                    await member.add_roles(premium_role, reason="Access Panel: email verified; paid subscriber")
                await _safe_ephemeral_send(interaction, "✅ Email verified ✅ Premium granted")
                return
            except discord.Forbidden:
                await _safe_ephemeral_send(
                    interaction,
                    "✅ Email verified.\n\n⚠️ Your email is paid, but I couldn’t grant Premium due to server permissions/role hierarchy. Please contact an administrator.",
                )
                return
        except Exception as e:
            logger.error(f"Error in Access Panel EnterCodeModal: {e}", exc_info=True)
            if guild:
                await _log_to_bot_logs(
                    bot,
                    guild,
                    title="❌ Access Panel: OTP Confirm Error",
                    description=(
                        f"**User:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                        f"**Email Hash:** `{_redact_hash(self.email_hash)}`\n"
                        f"**Error:** `{type(e).__name__}: {e}`"
                    ),
                    color=discord.Color.red(),
                )
            await _safe_ephemeral_send(
                interaction,
                "❌ An unexpected error occurred while confirming your code. Please try again or contact an administrator.",
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
        
        # Get verify channel ID
        access_panel_cog = interaction.client.get_cog("AccessPanelCog")
        verify_channel_id = None
        if access_panel_cog:
            verify_channel = await access_panel_cog.get_channel(interaction.guild, "verify")
            if verify_channel:
                verify_channel_id = verify_channel.id
        
        if verify_channel_id and interaction.channel_id != verify_channel_id:
            await interaction.response.send_message(
                f"❌ This panel can only be used in <#{verify_channel_id}>.",
                ephemeral=True
            )
            return
        
        # Show email modal
        view = AccessPanelView(interaction.client)
        modal = PremiumEmailModal(view)
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
        
        # Get verify channel ID
        access_panel_cog = interaction.client.get_cog("AccessPanelCog")
        verify_channel_id = None
        if access_panel_cog:
            verify_channel = await access_panel_cog.get_channel(interaction.guild, "verify")
            if verify_channel:
                verify_channel_id = verify_channel.id
        
        if verify_channel_id and interaction.channel_id != verify_channel_id:
            await interaction.followup.send(
                f"❌ This panel can only be used in <#{verify_channel_id}>.",
                ephemeral=True
            )
            return
        
        bot = interaction.client
        # Store email hash if provided
        claimed_email_hash = None
        if self.email.value:
            normalized = normalize_email(self.email.value)
            claimed_email_hash = hash_email(normalized)
        
        # Store pending request info temporarily
        verification_cog = bot.get_cog("VerificationQueueCog")
        if verification_cog:
            verification_cog.pending_requests[interaction.user.id] = {
                "email_hash": claimed_email_hash,
                "notes": self.notes.value if self.notes.value else None
            }
        
        # Ephemeral instructions + DM upload prompt (screenshots must never be public)
        view = UploadScreenshotDMView(bot)
        await interaction.followup.send(
            "✅ Request received!\n\n"
            "**Do not post screenshots in this channel.**\n"
            "**Your screenshot will only be visible to moderators.**\n\n"
            "Click the button below to receive a DM upload prompt.",
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
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.InteractionResponded:
            # Already responded, try followup
            pass
        
        try:
            guild = interaction.guild
            if not guild:
                await interaction.followup.send("❌ This can only be used in a server.", ephemeral=True)
                return
            
            bot = interaction.client
            
            # Enforce channel restriction
            access_panel_cog = bot.get_cog("AccessPanelCog")
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
            pending = await bot.db.get_pending_verify_request(interaction.user.id)
            if pending:
                await interaction.followup.send("❌ You already have a pending verification request.", ephemeral=True)
                return
            
            # Look for recent messages with attachments
            timeout_minutes = bot.config.PROOF_TIMEOUT_MINUTES
            cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)
            
            attachment_urls = []
            try:
                async for message in verify_channel_obj.history(limit=20, after=cutoff_time):
                    if message.author.id == interaction.user.id and message.attachments:
                        for attachment in message.attachments:
                            if attachment.content_type and attachment.content_type.startswith("image/"):
                                attachment_urls.append(attachment.url)
            except Exception as e:
                logger.error(f"Error reading channel history: {e}", exc_info=True)
                await interaction.followup.send("❌ Error reading channel history. Please try again.", ephemeral=True)
                return
            
            if not attachment_urls:
                await interaction.followup.send(
                    f"❌ No image attachments found in your recent messages. "
                    f"Please upload an image in {verify_channel_obj.mention} and try again.",
                    ephemeral=True
                )
                return
            
            # Get email hash from pending request
            verification_cog = bot.get_cog("VerificationQueueCog")
            claimed_email_hash = self.claimed_email_hash
            if verification_cog:
                pending_info = verification_cog.pending_requests.pop(interaction.user.id, {})
                if not claimed_email_hash:
                    claimed_email_hash = pending_info.get("email_hash")
            
            # Create verification request
            try:
                request_id = await bot.db.create_verify_request(
                    interaction.user.id,
                    claimed_email_hash,
                    attachment_urls
                )
            except Exception as e:
                logger.error(f"Error creating verify request: {e}", exc_info=True)
                await interaction.followup.send("❌ Error creating verification request. Please try again.", ephemeral=True)
                return
            
            # Record rate limit
            if verification_cog:
                verification_cog.rate_limiter.record_action(interaction.user.id, "verify_premium")
            
            # Post to verify queue
            admin_cog = bot.get_cog("AdminRolesCog")
            if admin_cog:
                queue_channel = await admin_cog.get_channel(guild, "verify_queue")
                if queue_channel:
                    try:
                        from cogs.verification_queue import VerifyQueueButtons
                        
                        embed = discord.Embed(
                            title="💎 New Premium Verification Request",
                            description=f"**User:** {interaction.user.mention} ({interaction.user})\n**User ID:** {interaction.user.id}",
                            color=discord.Color.blue(),
                            timestamp=datetime.now(timezone.utc)
                        )
                        
                        if claimed_email_hash:
                            is_paid = await bot.db.is_email_paid(claimed_email_hash)
                            email_status = "✅ In paid list" if is_paid else "❌ Not in paid list"
                            embed.add_field(name="Email Status", value=email_status, inline=True)
                        
                        if attachment_urls:
                            embed.add_field(
                                name="Proof Attachments",
                                value="\n".join([f"[Image {i+1}]({url})" for i, url in enumerate(attachment_urls[:5])]),
                                inline=False
                            )
                        
                        embed.set_footer(text=f"Request ID: {request_id}")
                        
                        view = VerifyQueueButtons(bot, request_id)
                        await queue_channel.send(embed=embed, view=view)
                        logger.info(f"Posted verification request {request_id} to verify-queue for user {interaction.user.id}")
                    except Exception as e:
                        logger.error(f"Error posting to verify queue: {e}", exc_info=True)
                        await interaction.followup.send(
                            "⚠️ Verification request created but failed to post to mod queue. Please contact an admin.",
                            ephemeral=True
                        )
                        return
                else:
                    logger.warning("verify_queue channel not found")
                    await interaction.followup.send(
                        "⚠️ Verification request created but verify_queue channel not found. Please contact an admin.",
                        ephemeral=True
                    )
                    return
            
            await interaction.followup.send(
                "✅ Your verification request has been submitted! A moderator will review it shortly.",
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Error in SubmitScreenshotButton callback: {e}", exc_info=True)
            try:
                await interaction.followup.send(
                    f"❌ An error occurred: {str(e)}. Please try again or contact an admin.",
                    ephemeral=True
                )
            except:
                pass


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
        
        # Get verify channel ID
        access_panel_cog = interaction.client.get_cog("AccessPanelCog")
        verify_channel_id = None
        if access_panel_cog:
            verify_channel = await access_panel_cog.get_channel(interaction.guild, "verify")
            if verify_channel:
                verify_channel_id = verify_channel.id
        
        if verify_channel_id and interaction.channel_id != verify_channel_id:
            await interaction.response.send_message(
                f"❌ This panel can only be used in <#{verify_channel_id}>.",
                ephemeral=True
            )
            return
        
        # Show screenshot modal
        view = AccessPanelView(interaction.client)
        modal = PremiumScreenshotModal(view)
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
        access_panel_cog = self.bot.get_cog("AccessPanelCog")
        if access_panel_cog:
            verify_channel = await access_panel_cog.get_channel(guild, "verify")
            if verify_channel:
                return verify_channel.id
        return None


class AccessPanelCog(commands.Cog):
    """Access Panel functionality."""
    
    def __init__(self, bot):
        self.bot = bot
        self.config = bot.config
        self.db = bot.db

    async def _is_mod_or_admin(self, member: discord.Member) -> bool:
        """Best-effort check for mod/admin privileges."""
        if member.guild_permissions.administrator or member.guild_permissions.manage_roles:
            return True
        admin_cog = self.bot.get_cog("AdminRolesCog")
        if not admin_cog:
            return False
        admin_role = await admin_cog.get_role(member.guild, "admin")
        mod_role = await admin_cog.get_role(member.guild, "mod")
        if admin_role and admin_role in member.roles:
            return True
        if mod_role and mod_role in member.roles:
            return True
        return False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Safety net: delete any screenshots accidentally posted in public #verify by non-mods."""
        if message.author.bot or not message.guild:
            return

        try:
            verify_channel = await self.get_channel(message.guild, "verify")
            if not verify_channel or message.channel.id != verify_channel.id:
                return

            if not message.attachments:
                return

            if not any(_attachment_is_image(a) for a in message.attachments):
                return

            member = message.author if isinstance(message.author, discord.Member) else message.guild.get_member(message.author.id)
            if isinstance(member, discord.Member) and await self._is_mod_or_admin(member):
                return

            try:
                await message.delete()
            except Exception:
                pass

            try:
                await message.author.send(
                    f"Hi! I removed your screenshot from {verify_channel.mention} to protect your privacy.\n\n"
                    "Please use the **Premium Access (Screenshot)** button in `#verify` and upload your screenshot via the DM prompt.\n\n"
                    "**Your screenshot will only be visible to moderators.**"
                )
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Error in AccessPanelCog.on_message screenshot guard: {e}", exc_info=True)
    
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
        
        try:
            verify_channel = await self.get_channel(guild, "verify")
            if not verify_channel:
                await interaction.followup.send("❌ Verify channel not found. Please create a channel named 'verify' or set CHANNEL_VERIFY_ID.", ephemeral=True)
                return
            
            # Check for existing panel (with timeout)
            existing_panel_id = None
            try:
                existing_panel_id = await self.db.get_access_panel_message_id(guild.id)
            except Exception as e:
                logger.warning(f"Failed to get existing panel ID: {e}")
            
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
                    "Submit proof of your Substack subscription for manual review.\n"
                    "**Your screenshot will only be visible to moderators.**\n\n"
                    "**⚠️ Important Disclaimer**\n"
                    "This bot and community do not provide financial advice. "
                    "All trading decisions are your own responsibility. "
                    "The bot only automates server administration tasks."
                ),
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc)
            )
            
            view = AccessPanelView(self.bot)
            
            if existing_panel_id:
                # Try to edit existing message
                try:
                    existing_message = await verify_channel.fetch_message(existing_panel_id)
                    await existing_message.edit(embed=embed, view=view)
                    try:
                        await self.db.update_access_panel_message_id(guild.id, existing_message.id)
                    except Exception as e:
                        logger.warning(f"Failed to update panel ID in DB: {e}")
                    await interaction.followup.send(f"✅ Access Panel updated in {verify_channel.mention}!", ephemeral=True)
                    return
                except discord.NotFound:
                    # Message was deleted, create new one
                    logger.info(f"Existing panel message {existing_panel_id} not found, creating new one")
                except discord.Forbidden:
                    await interaction.followup.send("❌ I don't have permission to edit messages in that channel.", ephemeral=True)
                    return
                except Exception as e:
                    logger.warning(f"Failed to edit existing panel: {e}")
                    # Continue to create new one
            
            # Post new panel
            try:
                message = await verify_channel.send(embed=embed, view=view)
                try:
                    await self.db.set_access_panel_message_id(guild.id, message.id)
                except Exception as e:
                    logger.warning(f"Failed to save panel ID to DB: {e}")
                await interaction.followup.send(f"✅ Access Panel posted in {verify_channel.mention}!", ephemeral=True)
            except discord.Forbidden:
                await interaction.followup.send("❌ I don't have permission to send messages in that channel.", ephemeral=True)
            except Exception as e:
                logger.error(f"Error posting access panel: {e}", exc_info=True)
                await interaction.followup.send(f"❌ Error posting panel: {str(e)}", ephemeral=True)
                
        except Exception as e:
            logger.error(f"Unexpected error in post_access_panel: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Unexpected error: {str(e)}", ephemeral=True)
