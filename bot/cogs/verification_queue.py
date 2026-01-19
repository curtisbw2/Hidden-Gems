"""Verification queue cog: mod approval workflow."""
import logging
from datetime import datetime, timedelta, timezone
from typing import List

import discord
from discord import app_commands
from discord.ext import commands

from services.hashing import hash_email, normalize_email
from services.rate_limit import RateLimiter

logger = logging.getLogger(__name__)

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

    def __init__(self, cog: "VerificationQueueCog"):
        super().__init__(label="Upload Screenshot (DM)", style=discord.ButtonStyle.primary)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.InteractionResponded:
            pass

        guild = interaction.guild
        if not guild:
            await interaction.followup.send("❌ This can only be used in a server.", ephemeral=True)
            return

        # Must have pending info from the modal
        pending_info = self.cog.pending_requests.get(interaction.user.id)
        if not pending_info:
            await interaction.followup.send(
                "❌ I don't see an active verification request. Please run `/verify_premium` again first.\n\n"
                "**Your screenshot will only be visible to moderators.**",
                ephemeral=True
            )
            return

        # Block if user already has a pending verification request in DB
        pending = await self.cog.db.get_pending_verify_request(interaction.user.id)
        if pending:
            await interaction.followup.send(
                "❌ You already have a pending verification request. Please wait for it to be reviewed.",
                ephemeral=True
            )
            return

        claimed_email_hash = pending_info.get("email_hash")

        # DM upload prompt
        try:
            dm = await interaction.user.create_dm()
            await dm.send(
                "🖼️ **Premium Verification – Upload**\n\n"
                "Please reply to this DM with your Substack subscription screenshot attached.\n\n"
                "**Important:** Do **not** post screenshots in the public `#verify` channel.\n"
                "**Your screenshot will only be visible to moderators.**\n\n"
                f"⏳ You have **{self.cog.config.PROOF_TIMEOUT_MINUTES} minutes** to upload."
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I couldn't DM you. Please enable DMs from this server and try again (or contact a moderator).",
                ephemeral=True
            )
            return

        await interaction.followup.send(
            "✅ Check your DMs for an upload prompt.\n\n**Your screenshot will only be visible to moderators.**",
            ephemeral=True
        )

        timeout_seconds = int(self.cog.config.PROOF_TIMEOUT_MINUTES * 60)

        def check(msg: discord.Message) -> bool:
            if msg.author.id != interaction.user.id:
                return False
            if msg.channel.id != dm.id:
                return False
            if not msg.attachments:
                return False
            return any(_attachment_is_image(a) for a in msg.attachments)

        try:
            proof_msg: discord.Message = await self.cog.bot.wait_for("message", timeout=timeout_seconds, check=check)
        except Exception:
            try:
                await dm.send(
                    "⏳ Upload timed out. Please go back to `#verify`, run `/verify_premium` again, and retry.\n\n"
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

        # Clear pending info now that we have proof
        pending_info = self.cog.pending_requests.pop(interaction.user.id, pending_info)

        # Process immediately (auto-approve if email is in paid list, else create mod-queue request)
        try:
            await self.cog.process_proof_submission(
                interaction=interaction,
                claimed_email_hash=claimed_email_hash,
                attachment_urls=attachment_urls
            )
        except Exception as e:
            logger.error(f"Error processing DM proof submission: {e}", exc_info=True)
            try:
                await dm.send("❌ An error occurred while submitting your proof. Please try again later or contact a moderator.")
            except Exception:
                pass


class UploadScreenshotDMView(discord.ui.View):
    """Ephemeral view that prompts DM-based upload."""

    def __init__(self, cog: "VerificationQueueCog"):
        super().__init__(timeout=600)
        self.add_item(UploadScreenshotDMButton(cog))


class VerifyModal(discord.ui.Modal, title="Premium Verification Request"):
    """Modal for verification request."""
    
    email = discord.ui.TextInput(
        label="Substack Email (Recommended)",
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
    
    def __init__(self, cog):
        super().__init__()
        self.cog = cog
    
    async def on_submit(self, interaction: discord.Interaction):
        """Handle modal submission."""
        await interaction.response.defer(ephemeral=True)
        
        # Store the email hash if provided
        claimed_email_hash = None
        if self.email.value:
            normalized = normalize_email(self.email.value)
            claimed_email_hash = hash_email(normalized)
        
        # Store pending request info temporarily
        self.cog.pending_requests[interaction.user.id] = {
            "email_hash": claimed_email_hash,
            "notes": self.notes.value if self.notes.value else None
        }

        # DM-based upload prompt (screenshots must never be public)
        view = UploadScreenshotDMView(self.cog)
        await interaction.followup.send(
            "✅ Request received!\n\n"
            "**Do not post screenshots in this channel.**\n"
            "**Your screenshot will only be visible to moderators.**\n\n"
            "Click the button below to receive a DM upload prompt.",
            view=view,
            ephemeral=True
        )


class VerifyQueueButtons(discord.ui.View):
    """Buttons for verification queue."""
    
    def __init__(self, bot, request_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.request_id = request_id
    
    async def check_mod_permissions(self, interaction: discord.Interaction) -> bool:
        """Check if user has mod/admin permissions."""
        if not interaction.guild:
            return False
        
        member = interaction.user
        guild = interaction.guild
        
        admin_role = await self.bot.cogs["AdminRolesCog"].get_role(guild, "admin")
        mod_role = await self.bot.cogs["AdminRolesCog"].get_role(guild, "mod")
        
        if admin_role and admin_role in member.roles:
            return True
        if mod_role and mod_role in member.roles:
            return True
        
        if member.guild_permissions.manage_roles:
            return True
        
        return False
    
    @discord.ui.button(label="✅ Approve", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Approve verification request."""
        if not await self.check_mod_permissions(interaction):
            await interaction.response.send_message("❌ You don't have permission to approve requests.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        cog = self.bot.cogs["VerificationQueueCog"]
        await cog.approve_request(self.request_id, interaction.user, interaction.guild)
        await interaction.followup.send("✅ Request approved!", ephemeral=True)
    
    @discord.ui.button(label="❌ Reject", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Reject verification request."""
        if not await self.check_mod_permissions(interaction):
            await interaction.response.send_message("❌ You don't have permission to reject requests.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        cog = self.bot.cogs["VerificationQueueCog"]
        await cog.reject_request(self.request_id, interaction.user, interaction.guild)
        await interaction.followup.send("✅ Request rejected.", ephemeral=True)
    
    @discord.ui.button(label="🧾 View Details", style=discord.ButtonStyle.secondary)
    async def view_details(self, interaction: discord.Interaction, button: discord.ui.Button):
        """View request details."""
        if not await self.check_mod_permissions(interaction):
            await interaction.response.send_message("❌ You don't have permission to view details.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        cog = self.bot.cogs["VerificationQueueCog"]
        request = await cog.get_request(self.request_id)
        
        if not request:
            await interaction.followup.send("❌ Request not found.", ephemeral=True)
            return
        
        embed = discord.Embed(
            title="📋 Verification Request Details",
            color=discord.Color.blue()
        )
        
        user = self.bot.get_user(request["discord_user_id"])
        embed.add_field(name="User", value=f"{user.mention if user else 'Unknown'} ({request['discord_user_id']})", inline=False)
        embed.add_field(name="Status", value=request["status"], inline=True)
        embed.add_field(name="Created", value=datetime.fromisoformat(request["created_at"]).strftime("%Y-%m-%d %H:%M UTC"), inline=True)
        
        if request.get("attachment_urls"):
            urls = "\n".join(request["attachment_urls"][:5])
            embed.add_field(name="Attachments", value=urls, inline=False)
        
        await interaction.followup.send(embed=embed, ephemeral=True)


class VerificationQueueCog(commands.Cog):
    """Verification queue functionality."""
    
    def __init__(self, bot):
        self.bot = bot
        self.config = bot.config
        self.db = bot.db
        self.rate_limiter = RateLimiter()
        self.pending_requests = {}  # Temporary storage: user_id -> (email_hash, notes)

    async def process_proof_submission(
        self,
        interaction: discord.Interaction,
        claimed_email_hash: str | None,
        attachment_urls: list[str]
    ):
        """
        Process proof submission (URL-only). Keeps existing queue logic unchanged:
        - Auto-grant Premium if email is in paid list
        - Otherwise create verify_request and post to #verify-queue with approve/reject buttons
        """
        guild = interaction.guild
        if not guild:
            return

        # Check if email is in paid subscribers list
        if claimed_email_hash:
            is_paid = await self.db.is_email_paid(claimed_email_hash)
            if is_paid:
                # Auto-grant Premium - email is in paid list!
                premium_role = await self.get_role(guild, "premium")
                if premium_role:
                    member = guild.get_member(interaction.user.id)
                    if member:
                        try:
                            await member.add_roles(premium_role, reason="Auto-approved: email in paid subscriber list")
                            logger.info(f"Auto-granted premium to {member} via DM proof (email in paid list)")

                            # Log to bot-logs
                            log_channel = await self.get_channel(guild, "bot_logs")
                            if log_channel:
                                embed = discord.Embed(
                                    title="✅ Premium Auto-Granted",
                                    description=(
                                        f"**User:** {member.mention} ({member})\n"
                                        f"**Method:** DM proof with email in paid subscriber list\n"
                                        f"**Email Hash:** {claimed_email_hash[:16]}..."
                                    ),
                                    color=discord.Color.green(),
                                    timestamp=datetime.now(timezone.utc)
                                )
                                try:
                                    await log_channel.send(embed=embed)
                                except Exception:
                                    pass

                            try:
                                await interaction.user.send(
                                    "✅ **Premium access granted!** Your email is in our paid subscriber list, so you've been automatically approved.\n\n"
                                    "**Your screenshot will only be visible to moderators.**"
                                )
                            except Exception:
                                pass
                            return
                        except Exception as e:
                            logger.error(f"Failed to auto-grant premium: {e}")
                            # Fall through to mod queue if auto-grant fails

        # Email not in paid list OR no email provided - go to mod queue
        request_id = await self.db.create_verify_request(
            interaction.user.id,
            claimed_email_hash,
            attachment_urls
        )

        # Record rate limit
        self.rate_limiter.record_action(interaction.user.id, "verify_premium")

        # Post to verify queue
        queue_channel = await self.get_channel(guild, "verify_queue")
        if queue_channel:
            embed = discord.Embed(
                title="💎 New Premium Verification Request",
                description=f"**User:** {interaction.user.mention} ({interaction.user})\n**User ID:** {interaction.user.id}",
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc)
            )

            if claimed_email_hash:
                is_paid = await self.db.is_email_paid(claimed_email_hash)
                email_status = "✅ In paid list" if is_paid else "❌ Not in paid list"
                embed.add_field(name="Email Status", value=email_status, inline=True)

            if attachment_urls:
                embed.add_field(
                    name="Proof Attachments",
                    value="\n".join([f"[Image {i+1}]({url})" for i, url in enumerate(attachment_urls[:5])]),
                    inline=False
                )
                embed.set_image(url=attachment_urls[0])

            embed.set_footer(text=f"Request ID: {request_id}")

            view = VerifyQueueButtons(self.bot, request_id)
            await queue_channel.send(f"<@&1457812443509035202>", embed=embed, view=view)

        # DM user (avoid public channel)
        try:
            await interaction.user.send(
                "✅ Your verification request has been submitted! A moderator will review it shortly.\n\n"
                "**Your screenshot will only be visible to moderators.**"
            )
        except Exception:
            pass
    
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
    
    @app_commands.command(name="verify_premium", description="Request Premium verification via mod queue")
    async def verify_premium(self, interaction: discord.Interaction):
        """Start verification request."""
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)
            return
        
        # Check if in verify channel
        verify_channel = await self.get_channel(guild, "verify")
        if verify_channel and interaction.channel_id != verify_channel.id:
            await interaction.response.send_message(
                f"❌ This command can only be used in {verify_channel.mention}.",
                ephemeral=True
            )
            return
        
        # Check rate limit
        allowed, next_time = self.rate_limiter.check_rate_limit(
            interaction.user.id,
            "verify_premium",
            self.config.VERIFY_COOLDOWN_MINUTES
        )
        
        if not allowed:
            if next_time:
                wait_minutes = int((next_time - datetime.now(timezone.utc)).total_seconds() / 60)
                await interaction.response.send_message(
                    f"⏳ Please wait {wait_minutes} minutes before submitting another verification request.",
                    ephemeral=True
                )
            return
        
        # Check for pending request
        pending = await self.db.get_pending_verify_request(interaction.user.id)
        if pending:
            await interaction.response.send_message(
                "❌ You already have a pending verification request. Please wait for it to be reviewed.",
                ephemeral=True
            )
            return
        
        # Show modal
        modal = VerifyModal(self)
        await interaction.response.send_modal(modal)
        
        # Store pending info temporarily
        # We'll handle this in submit_proof
    
    @app_commands.command(name="submit_proof", description="Submit proof attachment for verification")
    async def submit_proof(self, interaction: discord.Interaction):
        """Submit proof attachment (DM-based; screenshots are never public)."""
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if not guild:
            await interaction.followup.send("❌ This command can only be used in a server.", ephemeral=True)
            return

        # Check if in verify channel (keep restriction, but do NOT collect proof here)
        verify_channel = await self.get_channel(guild, "verify")
        if verify_channel and interaction.channel_id != verify_channel.id:
            await interaction.followup.send(
                f"❌ This command can only be used in {verify_channel.mention}.",
                ephemeral=True
            )
            return

        # Must have pending info from the modal
        if interaction.user.id not in self.pending_requests:
            await interaction.followup.send(
                "❌ I don't see an active verification request. Please run `/verify_premium` first.\n\n"
                "**Your screenshot will only be visible to moderators.**",
                ephemeral=True
            )
            return

        view = UploadScreenshotDMView(self)
        await interaction.followup.send(
            "**Do not post screenshots in this channel.**\n"
            "**Your screenshot will only be visible to moderators.**\n\n"
            "Click the button below to receive a DM upload prompt.",
            view=view,
            ephemeral=True
        )
    
    async def get_request(self, request_id: int):
        """Get verification request by ID."""
        async with self.db.get_connection() as db:
            async with db.execute(
                "SELECT * FROM verify_requests WHERE id = ?",
                (request_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    import json
                    result = dict(row)
                    if result.get("attachment_urls"):
                        result["attachment_urls"] = json.loads(result["attachment_urls"])
                    return result
                return None
    
    async def approve_request(self, request_id: int, reviewer: discord.Member, guild: discord.Guild):
        """Approve a verification request."""
        request = await self.db.approve_verify_request(request_id, reviewer.id)
        if not request:
            return
        
        user_id = request["discord_user_id"]
        user = guild.get_member(user_id)
        
        if not user:
            logger.warning(f"User {user_id} not found in guild")
            return
        
        # Grant Premium role
        premium_role = await self.get_role(guild, "premium")
        if premium_role:
            try:
                await user.add_roles(premium_role, reason="Approved via verification queue")
                logger.info(f"Granted premium to {user} via verification queue")
            except Exception as e:
                logger.error(f"Failed to grant premium role: {e}")
        
        # Log to bot-logs
        log_channel = await self.get_channel(guild, "bot_logs")
        if log_channel:
            embed = discord.Embed(
                title="✅ Premium Verification Approved",
                description=(
                    f"**User:** {user.mention} ({user})\n"
                    f"**Reviewed by:** {reviewer.mention} ({reviewer})\n"
                    f"**Request ID:** {request_id}"
                ),
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )
            try:
                await log_channel.send(embed=embed)
            except Exception as e:
                logger.error(f"Failed to log approval: {e}")
        
        # Try to delete proof messages
        verify_channel = await self.get_channel(guild, "verify")
        if verify_channel:
            cutoff_time = datetime.fromisoformat(request["created_at"]) - timedelta(minutes=5)
            deleted_count = 0
            async for message in verify_channel.history(limit=50, after=cutoff_time):
                if message.author.id == user_id and message.attachments:
                    try:
                        await message.delete()
                        deleted_count += 1
                    except Exception:
                        pass
            
            if deleted_count == 0 and log_channel:
                await log_channel.send(f"⚠️ Could not delete proof messages for {user.mention}. Please delete manually.")
        
        # DM user
        try:
            await user.send(
                "✅ Your Premium verification has been approved! You now have access to Premium Member features."
            )
        except Exception:
            pass
    
    async def reject_request(self, request_id: int, reviewer: discord.Member, guild: discord.Guild):
        """Reject a verification request."""
        await self.db.reject_verify_request(request_id, reviewer.id, "Rejected by moderator")
        
        # Get request info
        request = await self.get_request(request_id)
        if not request:
            return
        
        user_id = request["discord_user_id"]
        user = guild.get_member(user_id)
        
        if not user:
            return
        
        # Log to bot-logs
        log_channel = await self.get_channel(guild, "bot_logs")
        if log_channel:
            embed = discord.Embed(
                title="❌ Premium Verification Rejected",
                description=(
                    f"**User:** {user.mention} ({user})\n"
                    f"**Reviewed by:** {reviewer.mention} ({reviewer})\n"
                    f"**Request ID:** {request_id}"
                ),
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc)
            )
            try:
                await log_channel.send(embed=embed)
            except Exception as e:
                logger.error(f"Failed to log rejection: {e}")
        
        # DM user
        try:
            await user.send(
                "❌ Your Premium verification request has been rejected. "
                "If you believe this is an error, please contact a moderator or try again later."
            )
        except Exception:
            pass
