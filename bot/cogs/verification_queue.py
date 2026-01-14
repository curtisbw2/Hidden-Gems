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
        
        # Check if email is in paid list (for user feedback)
        message = f"✅ Request received! Please attach your proof image in your next message in this channel "
        message += f"within {self.cog.config.PROOF_TIMEOUT_MINUTES} minutes, then run `/submit_proof`.\n\n"
        
        if claimed_email_hash:
            is_paid = await self.cog.db.is_email_paid(claimed_email_hash)
            if is_paid:
                message += "💡 **Good news!** Your email is in our paid subscriber list. After you submit proof, you'll be auto-approved!"
            else:
                message += "ℹ️ Your email wasn't found in our paid subscriber list. A moderator will review your request."
        else:
            message += "ℹ️ **Tip:** Providing your email helps us verify faster if you're in our paid subscriber list."
        
        await interaction.followup.send(message, ephemeral=True)


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
        """Submit proof attachment."""
        await interaction.response.defer(ephemeral=True)
        
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("❌ This command can only be used in a server.", ephemeral=True)
            return
        
        # Check if in verify channel
        verify_channel = await self.get_channel(guild, "verify")
        if verify_channel and interaction.channel_id != verify_channel.id:
            await interaction.followup.send(
                f"❌ This command can only be used in {verify_channel.mention}.",
                ephemeral=True
            )
            return
        
        # Check for pending request
        pending = await self.db.get_pending_verify_request(interaction.user.id)
        if pending:
            await interaction.followup.send(
                "❌ You already have a pending verification request. Please wait for it to be reviewed.",
                ephemeral=True
            )
            return
        
        # Look for recent messages with attachments
        timeout_minutes = self.config.PROOF_TIMEOUT_MINUTES
        cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)
        
        attachment_urls = []
        async for message in verify_channel.history(limit=20, after=cutoff_time):
            if message.author.id == interaction.user.id and message.attachments:
                for attachment in message.attachments:
                    if attachment.content_type and attachment.content_type.startswith("image/"):
                        attachment_urls.append(attachment.url)
        
        if not attachment_urls:
            await interaction.followup.send(
                f"❌ No image attachments found in your recent messages. "
                f"Please upload an image in {verify_channel.mention} and try again.",
                ephemeral=True
            )
            return
        
        # Get email hash from pending request (stored from modal)
        pending_info = self.pending_requests.pop(interaction.user.id, {})
        claimed_email_hash = pending_info.get("email_hash")
        
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
                            logger.info(f"Auto-granted premium to {member} via verify_premium (email in paid list)")
                            
                            # Log to bot-logs
                            log_channel = await self.get_channel(guild, "bot_logs")
                            if log_channel:
                                embed = discord.Embed(
                                    title="✅ Premium Auto-Granted",
                                    description=(
                                        f"**User:** {member.mention} ({member})\n"
                                        f"**Method:** `/verify_premium` with email in paid subscriber list\n"
                                        f"**Email Hash:** {claimed_email_hash[:16]}..."
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
                            # Fall through to mod queue if auto-grant fails
        
        # Email not in paid list OR no email provided - go to mod queue
        # Create verification request
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
                # Check if email is in paid list (for mod reference)
                is_paid = await self.db.is_email_paid(claimed_email_hash)
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
            "✅ Your verification request has been submitted! A moderator will review it shortly.\n\n"
            "**Note:** If your email is in our paid subscriber list, you would have been auto-approved. "
            "Otherwise, please wait for manual review.",
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
