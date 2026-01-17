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

    def _redact_hash(self, h: str | None) -> str:
        if not h:
            return "None"
        if len(h) <= 12:
            return h
        return f"{h[:8]}…{h[-6:]}"

    def _ensure_guild_ok(self, interaction: discord.Interaction) -> tuple[bool, str | None]:
        """Ensure command is run in a guild and (optionally) in the configured guild."""
        if not interaction.guild:
            return False, "❌ Please run this command in the server (not in DMs)."
        if self.config.GUILD_ID and interaction.guild_id != self.config.GUILD_ID:
            return False, "❌ Please run this command in the Hidden Gems server."
        return True, None

    async def check_admin_permissions(self, interaction: discord.Interaction) -> bool:
        """Admin-only: Admin role or Discord Administrator permission."""
        if not interaction.guild:
            return False
        member = interaction.user
        admin_role = await self.get_role(interaction.guild, "admin")
        if admin_role and admin_role in getattr(member, "roles", []):
            return True
        if getattr(member, "guild_permissions", None) and member.guild_permissions.administrator:
            return True
        return False

    async def _log_to_bot_logs(
        self,
        guild: discord.Guild,
        title: str,
        description: str,
        color: discord.Color = discord.Color.blurple(),
    ) -> None:
        """Best-effort log to #bot-logs."""
        try:
            log_channel = await self.get_channel(guild, "bot_logs")
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
            # Never break user flow because logging failed
            pass

    def _as_utc_datetime(self, value) -> datetime | None:
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
        ok, msg = self._ensure_guild_ok(interaction)
        if not ok:
            await interaction.followup.send(msg, ephemeral=True)
            return
        
        if not self.email_service.enabled:
            await interaction.followup.send(
                "❌ Email service is not configured. Please contact an administrator.",
                ephemeral=True
            )
            return
        try:
            guild = interaction.guild
            assert guild is not None

            # Normalize and hash email
            normalized = normalize_email(email)
            email_hash = hash_email(normalized)

            await self._log_to_bot_logs(
                guild,
                title="📩 OTP Link Requested",
                description=(
                    f"**User:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                    f"**Email Hash:** `{self._redact_hash(email_hash)}`"
                ),
                color=discord.Color.blurple(),
            )

            # Check if email is already linked to another user
            existing_user = await self.db.get_user_by_email_hash(email_hash)
            if existing_user and existing_user.get("discord_user_id") != interaction.user.id:
                await self._log_to_bot_logs(
                    guild,
                    title="❌ OTP Link Blocked",
                    description=(
                        f"**User:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                        f"**Email Hash:** `{self._redact_hash(email_hash)}`\n"
                        f"**Reason:** Email already linked to another account (`{existing_user.get('discord_user_id')}`)"
                    ),
                    color=discord.Color.red(),
                )
                await interaction.followup.send(
                    "❌ This email is already linked to another Discord account.",
                    ephemeral=True,
                )
                return

            # Generate OTP
            otp_code, otp_hash = generate_otp_code()
            expires_at = datetime.now(timezone.utc) + timedelta(minutes=self.config.OTP_EXPIRY_MINUTES)

            # Store OTP with email hash (best to replace any previous OTP for same user+email)
            await self.db.delete_otps_for_user_email(interaction.user.id, email_hash)
            await self.db.store_otp(interaction.user.id, otp_hash, email_hash, expires_at)

            # Send email
            email_sent = await self.email_service.send_otp(normalized, otp_code)

            if not email_sent:
                await self._log_to_bot_logs(
                    guild,
                    title="❌ OTP Email Failed",
                    description=(
                        f"**User:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                        f"**Email Hash:** `{self._redact_hash(email_hash)}`\n"
                        f"**Reason:** SendGrid send_otp returned False"
                    ),
                    color=discord.Color.red(),
                )
                await interaction.followup.send(
                    "❌ Failed to send verification email. Please try again later or contact support.",
                    ephemeral=True,
                )
                return

            await self._log_to_bot_logs(
                guild,
                title="✅ OTP Email Sent",
                description=(
                    f"**User:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                    f"**Email Hash:** `{self._redact_hash(email_hash)}`\n"
                    f"**Expires At:** `{expires_at.isoformat()}`"
                ),
                color=discord.Color.green(),
            )

            await interaction.followup.send(
                "✅ Code sent. Run `/confirm_code <code> <email>` to verify.\n\n"
                f"**Note:** The code expires in {self.config.OTP_EXPIRY_MINUTES} minutes.",
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"Error in /link_email: {e}", exc_info=True)
            if interaction.guild:
                await self._log_to_bot_logs(
                    interaction.guild,
                    title="❌ OTP Link Error",
                    description=(
                        f"**User:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                        f"**Error:** `{type(e).__name__}: {e}`"
                    ),
                    color=discord.Color.red(),
                )
            await interaction.followup.send(
                "❌ An unexpected error occurred while sending your code. Please try again or contact an administrator.",
                ephemeral=True,
            )
    
    @app_commands.command(name="confirm_code", description="Confirm your email with the verification code")
    @app_commands.describe(code="The 6-digit verification code sent to your email", email="Your email address")
    async def confirm_code(self, interaction: discord.Interaction, code: str, email: str):
        """Confirm OTP code."""
        await interaction.response.defer(ephemeral=True)
        ok, msg = self._ensure_guild_ok(interaction)
        if not ok:
            await interaction.followup.send(msg, ephemeral=True)
            return
        
        # Validate code format
        if not code.isdigit() or len(code) != 6:
            await interaction.followup.send("❌ Invalid code format. Please enter a 6-digit number.", ephemeral=True)
            return
        try:
            guild = interaction.guild
            assert guild is not None

            # Normalize and hash email
            normalized = normalize_email(email)
            email_hash = hash_email(normalized)

            await self._log_to_bot_logs(
                guild,
                title="🔐 OTP Confirm Requested",
                description=(
                    f"**User:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                    f"**Email Hash:** `{self._redact_hash(email_hash)}`"
                ),
                color=discord.Color.blurple(),
            )

            # Hash code input
            input_code_hash = hash_otp_code(code)

            # Fetch latest unexpired OTP for this user+email
            otp_record = await self.db.get_otp_by_email_hash(interaction.user.id, email_hash)
            if not otp_record:
                await self._log_to_bot_logs(
                    guild,
                    title="❌ OTP Confirm Failed",
                    description=(
                        f"**User:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                        f"**Email Hash:** `{self._redact_hash(email_hash)}`\n"
                        f"**Stage:** Load OTP\n"
                        f"**Result:** No unexpired OTP record found"
                    ),
                    color=discord.Color.red(),
                )
                await interaction.followup.send(
                    "❌ No active code found for that email. Please run `/link_email <email>` again to get a new code.",
                    ephemeral=True,
                )
                return

            stored_code_hash = otp_record.get("code_hash")
            stored_email_hash = otp_record.get("email_hash")
            attempts = int(otp_record.get("attempts") or 0)
            expires_at = self._as_utc_datetime(otp_record.get("expires_at"))

            await self._log_to_bot_logs(
                guild,
                title="📦 OTP Loaded",
                description=(
                    f"**User:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                    f"**Email Hash:** `{self._redact_hash(email_hash)}`\n"
                    f"**Stored Code Hash:** `{self._redact_hash(stored_code_hash)}`\n"
                    f"**Attempts:** `{attempts}`\n"
                    f"**Expires At:** `{expires_at.isoformat() if expires_at else 'Unknown'}`"
                ),
                color=discord.Color.blurple(),
            )

            # Basic consistency check
            if stored_email_hash != email_hash:
                await self._log_to_bot_logs(
                    guild,
                    title="❌ OTP Confirm Failed",
                    description=(
                        f"**User:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                        f"**Email Hash:** `{self._redact_hash(email_hash)}`\n"
                        f"**Stage:** Validate\n"
                        f"**Result:** Email hash mismatch on OTP row"
                    ),
                    color=discord.Color.red(),
                )
                await interaction.followup.send(
                    "❌ Email address doesn't match the active code. Please use the same email you used with `/link_email`.",
                    ephemeral=True,
                )
                return

            # Expiry check (defensive: DB query should already filter unexpired)
            now = datetime.now(timezone.utc)
            if not expires_at or now > expires_at:
                await self.db.delete_otps_for_user_email(interaction.user.id, email_hash)
                await self._log_to_bot_logs(
                    guild,
                    title="⌛ OTP Expired",
                    description=(
                        f"**User:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                        f"**Email Hash:** `{self._redact_hash(email_hash)}`\n"
                        f"**Stage:** Validate\n"
                        f"**Result:** Expired (or unreadable expires_at); OTP cleared"
                    ),
                    color=discord.Color.orange(),
                )
                await interaction.followup.send(
                    "❌ Code has expired. Please request a new code with `/link_email <email>`.",
                    ephemeral=True,
                )
                return

            # Too many attempts check (lock)
            if attempts >= self.config.OTP_MAX_ATTEMPTS:
                await self.db.delete_otps_for_user_email(interaction.user.id, email_hash)
                await self._log_to_bot_logs(
                    guild,
                    title="🔒 OTP Locked",
                    description=(
                        f"**User:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                        f"**Email Hash:** `{self._redact_hash(email_hash)}`\n"
                        f"**Stage:** Validate\n"
                        f"**Result:** attempts={attempts} >= max={self.config.OTP_MAX_ATTEMPTS}; OTP cleared"
                    ),
                    color=discord.Color.red(),
                )
                await interaction.followup.send(
                    "❌ Too many failed attempts. Please request a new code with `/link_email <email>`.",
                    ephemeral=True,
                )
                return

            # Code check (increment attempts on failure)
            if not stored_code_hash or input_code_hash != stored_code_hash:
                await self.db.increment_otp_attempts(interaction.user.id, stored_code_hash)
                new_record = await self.db.get_otp(interaction.user.id, stored_code_hash)
                new_attempts = int((new_record or {}).get("attempts") or (attempts + 1))
                remaining = max(self.config.OTP_MAX_ATTEMPTS - new_attempts, 0)

                if new_attempts >= self.config.OTP_MAX_ATTEMPTS:
                    await self.db.delete_otps_for_user_email(interaction.user.id, email_hash)
                    await self._log_to_bot_logs(
                        guild,
                        title="🔒 OTP Locked (Invalid Code)",
                        description=(
                            f"**User:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                            f"**Email Hash:** `{self._redact_hash(email_hash)}`\n"
                            f"**Stage:** Validate\n"
                            f"**Result:** Invalid code; attempts={new_attempts}; OTP cleared"
                        ),
                        color=discord.Color.red(),
                    )
                    await interaction.followup.send(
                        "❌ Incorrect code. You’ve reached the maximum attempts. Please run `/link_email <email>` to get a new code.",
                        ephemeral=True,
                    )
                    return

                await self._log_to_bot_logs(
                    guild,
                    title="❌ OTP Invalid Code",
                    description=(
                        f"**User:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                        f"**Email Hash:** `{self._redact_hash(email_hash)}`\n"
                        f"**Stage:** Validate\n"
                        f"**Result:** Invalid code; attempts={new_attempts}; remaining={remaining}"
                    ),
                    color=discord.Color.orange(),
                )
                await interaction.followup.send(
                    f"❌ Incorrect code. Please try again. **Attempts remaining:** {remaining}\n\n"
                    "If you think the code expired, run `/link_email <email>` again.",
                    ephemeral=True,
                )
                return

            await self._log_to_bot_logs(
                guild,
                title="✅ OTP Validated",
                description=(
                    f"**User:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                    f"**Email Hash:** `{self._redact_hash(email_hash)}`\n"
                    f"**Stage:** Validate\n"
                    f"**Result:** Code hash matches"
                ),
                color=discord.Color.green(),
            )

            # Mark verified
            await self.db.create_user(interaction.user.id)
            await self.db.link_email(interaction.user.id, email_hash)
            await self.db.delete_otps_for_user_email(interaction.user.id, email_hash)

            await self._log_to_bot_logs(
                guild,
                title="🗄️ User Verified (DB Updated)",
                description=(
                    f"**User:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                    f"**Email Hash:** `{self._redact_hash(email_hash)}`\n"
                    f"**Stage:** DB Update\n"
                    f"**Result:** users.email_verified=true; OTP cleared"
                ),
                color=discord.Color.green(),
            )

            # Check paid
            is_paid = await self.db.is_email_paid(email_hash)

            await self._log_to_bot_logs(
                guild,
                title="💳 Paid Email Check",
                description=(
                    f"**User:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                    f"**Email Hash:** `{self._redact_hash(email_hash)}`\n"
                    f"**Stage:** Paid Check\n"
                    f"**Active Paid:** `{is_paid}`"
                ),
                color=discord.Color.green() if is_paid else discord.Color.blurple(),
            )

            # Role assignment if paid
            if is_paid:
                premium_role = await self.get_role(guild, "premium")
                if not premium_role:
                    await self._log_to_bot_logs(
                        guild,
                        title="⚠️ Premium Role Missing",
                        description=(
                            f"**User:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                            f"**Stage:** Role Assignment\n"
                            f"**Result:** ROLE_PREMIUM not found (by ID or name)"
                        ),
                        color=discord.Color.orange(),
                    )
                    await interaction.followup.send(
                        "✅ Email verified.\n\n"
                        "⚠️ I couldn’t grant Premium automatically because the Premium role wasn’t found. Please contact an administrator.",
                        ephemeral=True,
                    )
                    return

                try:
                    member = guild.get_member(interaction.user.id)
                    if not member:
                        member = await guild.fetch_member(interaction.user.id)
                except Exception:
                    member = None

                if not member:
                    await self._log_to_bot_logs(
                        guild,
                        title="⚠️ Member Not Found",
                        description=(
                            f"**User ID:** `{interaction.user.id}`\n"
                            f"**Stage:** Role Assignment\n"
                            f"**Result:** Could not resolve guild member to assign role"
                        ),
                        color=discord.Color.orange(),
                    )
                    await interaction.followup.send(
                        "✅ Email verified ✅\n\n"
                        "⚠️ I couldn’t grant Premium automatically (couldn’t find your server member record). Please contact an administrator.",
                        ephemeral=True,
                    )
                    return

                await self._log_to_bot_logs(
                    guild,
                    title="🎭 Premium Role Assignment Attempt",
                    description=(
                        f"**User:** {member.mention} (`{member.id}`)\n"
                        f"**Role:** {premium_role.mention} (`{premium_role.id}`)\n"
                        f"**Stage:** Role Assignment\n"
                        f"**Result:** Attempting add_roles"
                    ),
                    color=discord.Color.blurple(),
                )

                try:
                    if premium_role in member.roles:
                        await interaction.followup.send(
                            "✅ Email verified ✅ Premium granted",
                            ephemeral=True,
                        )
                        await self._log_to_bot_logs(
                            guild,
                            title="✅ Premium Already Present",
                            description=(
                                f"**User:** {member.mention} (`{member.id}`)\n"
                                f"**Role:** {premium_role.mention}\n"
                                f"**Stage:** Role Assignment\n"
                                f"**Result:** Member already had role"
                            ),
                            color=discord.Color.green(),
                        )
                        return

                    await member.add_roles(
                        premium_role,
                        reason="Email verified; email_hash is active in paid_emails",
                    )
                    logger.info(f"Auto-granted premium to {member} via email linking")
                    await self._log_to_bot_logs(
                        guild,
                        title="✅ Premium Granted",
                        description=(
                            f"**User:** {member.mention} (`{member.id}`)\n"
                            f"**Role:** {premium_role.mention} (`{premium_role.id}`)\n"
                            f"**Stage:** Role Assignment\n"
                            f"**Result:** add_roles success"
                        ),
                        color=discord.Color.green(),
                    )
                    await interaction.followup.send(
                        "✅ Email verified ✅ Premium granted",
                        ephemeral=True,
                    )
                    return
                except discord.Forbidden:
                    await self._log_to_bot_logs(
                        guild,
                        title="❌ Premium Grant Failed (Forbidden)",
                        description=(
                            f"**User:** {member.mention} (`{member.id}`)\n"
                            f"**Role:** {premium_role.mention} (`{premium_role.id}`)\n"
                            f"**Stage:** Role Assignment\n"
                            f"**Result:** Missing permissions / role hierarchy issue"
                        ),
                        color=discord.Color.red(),
                    )
                    await interaction.followup.send(
                        "✅ Email verified.\n\n"
                        "⚠️ Your email is paid, but I couldn’t grant Premium automatically due to server permissions/role hierarchy. Please contact an administrator.",
                        ephemeral=True,
                    )
                    return
                except Exception as e:
                    await self._log_to_bot_logs(
                        guild,
                        title="❌ Premium Grant Failed (Error)",
                        description=(
                            f"**User:** {member.mention} (`{member.id}`)\n"
                            f"**Stage:** Role Assignment\n"
                            f"**Error:** `{type(e).__name__}: {e}`"
                        ),
                        color=discord.Color.red(),
                    )
                    logger.error(f"Failed to auto-grant premium: {e}", exc_info=True)
                    await interaction.followup.send(
                        "✅ Email verified.\n\n"
                        "⚠️ Your email is paid, but an error occurred while granting Premium automatically. Please contact an administrator.",
                        ephemeral=True,
                    )
                    return

            # Not paid
            await interaction.followup.send(
                "✅ Email verified ✅ Premium will be granted after next subscriber sync",
                ephemeral=True,
            )
            await self._log_to_bot_logs(
                guild,
                title="✅ Email Verified (Not Paid Yet)",
                description=(
                    f"**User:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                    f"**Email Hash:** `{self._redact_hash(email_hash)}`\n"
                    f"**Stage:** Complete\n"
                    f"**Result:** Verified; not in active paid_emails"
                ),
                color=discord.Color.green(),
            )
        except Exception as e:
            logger.error(f"Error in /confirm_code: {e}", exc_info=True)
            if interaction.guild:
                await self._log_to_bot_logs(
                    interaction.guild,
                    title="❌ OTP Confirm Error",
                    description=(
                        f"**User:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                        f"**Error:** `{type(e).__name__}: {e}`"
                    ),
                    color=discord.Color.red(),
                )
            await interaction.followup.send(
                "❌ An unexpected error occurred while confirming your code. Please try again or contact an administrator.",
                ephemeral=True,
            )

    @app_commands.command(name="otp_debug", description="Debug email OTP state for a user (Admin only)")
    @app_commands.describe(user="User to inspect", email="Optional email to check paid/OTP state (will be hashed)")
    async def otp_debug(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
        email: str | None = None,
    ):
        """Admin-only OTP debug command. Never reveals raw email or raw OTP."""
        if not await self.check_admin_permissions(interaction):
            await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        ok, msg = self._ensure_guild_ok(interaction)
        if not ok:
            await interaction.followup.send(msg, ephemeral=True)
            return

        guild = interaction.guild
        assert guild is not None

        target = user or interaction.user

        # Compute optional email hash
        email_hash = None
        if email:
            email_hash = hash_email(normalize_email(email))

        try:
            user_row = await self.db.get_user(target.id)
            linked_email_hash = user_row.get("email_hash") if user_row else None
            email_verified = bool(user_row.get("email_verified")) if user_row else False

            # OTP row: if we have an email hash (provided or linked), check latest unexpired
            otp_row = None
            otp_email_hash = email_hash or linked_email_hash
            if otp_email_hash:
                otp_row = await self.db.get_otp_by_email_hash(target.id, otp_email_hash)

            # Paid check: if we have an email hash (provided or linked)
            paid_active = None
            paid_email_hash = email_hash or linked_email_hash
            if paid_email_hash:
                paid_active = await self.db.is_email_paid(paid_email_hash)

            premium_role = await self.get_role(guild, "premium")
            has_premium = bool(premium_role and premium_role in target.roles)

            # Build response
            lines = [
                f"**User:** {target.mention} (`{target.id}`)",
                f"**Linked Email Hash:** `{self._redact_hash(linked_email_hash)}`",
                f"**Email Verified:** `{email_verified}`",
                f"**Provided Email Hash:** `{self._redact_hash(email_hash)}`",
                f"**Paid Active (linked/provided):** `{paid_active}`" if paid_active is not None else "**Paid Active (linked/provided):** `Unknown (no email hash)`",
                f"**Premium Role Present:** `{has_premium}`",
                f"**Premium Role Found:** `{bool(premium_role)}`",
            ]

            if otp_row:
                exp = self._as_utc_datetime(otp_row.get("expires_at"))
                lines += [
                    "**OTP Row:** `Present`",
                    f"**OTP Email Hash:** `{self._redact_hash(otp_row.get('email_hash'))}`",
                    f"**OTP Code Hash:** `{self._redact_hash(otp_row.get('code_hash'))}`",
                    f"**OTP Attempts:** `{otp_row.get('attempts')}`",
                    f"**OTP Expires At:** `{exp.isoformat() if exp else 'Unknown'}`",
                ]
            else:
                lines += ["**OTP Row:** `None (no unexpired OTP found)`"]

            await interaction.followup.send("\n".join(lines), ephemeral=True)
        except Exception as e:
            logger.error(f"Error in /otp_debug: {e}", exc_info=True)
            await self._log_to_bot_logs(
                guild,
                title="❌ OTP Debug Error",
                description=f"**Moderator:** {interaction.user.mention} (`{interaction.user.id}`)\n**Error:** `{type(e).__name__}: {e}`",
                color=discord.Color.red(),
            )
            await interaction.followup.send("❌ An error occurred while running otp_debug.", ephemeral=True)

    @app_commands.command(name="otp_force_verify", description="Force verify a user's email hash (Admin only)")
    @app_commands.describe(user="User to force verify", email="Email to hash and link (will not be stored raw)")
    async def otp_force_verify(self, interaction: discord.Interaction, user: discord.Member, email: str):
        """Admin-only emergency: mark email_verified=true for a user with the provided email hash."""
        if not await self.check_admin_permissions(interaction):
            await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        ok, msg = self._ensure_guild_ok(interaction)
        if not ok:
            await interaction.followup.send(msg, ephemeral=True)
            return

        guild = interaction.guild
        assert guild is not None

        email_hash = hash_email(normalize_email(email))

        try:
            await self.db.create_user(user.id)
            await self.db.link_email(user.id, email_hash)
            await self.db.delete_otps_for_user_email(user.id, email_hash)

            await self._log_to_bot_logs(
                guild,
                title="🛠️ OTP Force Verify",
                description=(
                    f"**Moderator:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                    f"**Target:** {user.mention} (`{user.id}`)\n"
                    f"**Email Hash:** `{self._redact_hash(email_hash)}`\n"
                    f"**Result:** users.email_verified=true"
                ),
                color=discord.Color.orange(),
            )
            await interaction.followup.send(
                f"✅ Forced email verification for {user.mention}.\n**Email Hash:** `{self._redact_hash(email_hash)}`",
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"Error in /otp_force_verify: {e}", exc_info=True)
            await self._log_to_bot_logs(
                guild,
                title="❌ OTP Force Verify Error",
                description=(
                    f"**Moderator:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                    f"**Target:** {user.mention} (`{user.id}`)\n"
                    f"**Email Hash:** `{self._redact_hash(email_hash)}`\n"
                    f"**Error:** `{type(e).__name__}: {e}`"
                ),
                color=discord.Color.red(),
            )
            await interaction.followup.send("❌ Failed to force verify user. Check bot logs.", ephemeral=True)
    
