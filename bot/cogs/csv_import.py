"""CSV import cog: paid subscriber import and sync."""
import logging
import csv
import io
from datetime import datetime, timezone
from typing import List

import discord
from discord import app_commands
from discord.ext import commands

from services.hashing import hash_email, normalize_email

logger = logging.getLogger(__name__)


class CSVImportCog(commands.Cog):
    """CSV import functionality."""
    
    def __init__(self, bot):
        self.bot = bot
        self.config = bot.config
        self.db = bot.db
    
    async def check_admin_permissions(self, interaction: discord.Interaction) -> bool:
        """Check if user has admin permissions."""
        if not interaction.guild:
            return False
        
        member = interaction.user
        guild = interaction.guild
        
        admin_role = await self.get_role(guild, "admin")
        if admin_role and admin_role in member.roles:
            return True
        
        if member.guild_permissions.administrator:
            return True
        
        return False
    
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
    
    def parse_csv(self, csv_content: bytes) -> List[str]:
        """Parse CSV and extract email addresses. Returns list of email hashes."""
        try:
            # Try to decode as UTF-8
            text = csv_content.decode("utf-8")
        except UnicodeDecodeError:
            # Try other encodings
            try:
                text = csv_content.decode("latin-1")
            except Exception:
                text = csv_content.decode("utf-8", errors="ignore")
        
        reader = csv.DictReader(io.StringIO(text))
        
        # Find email column (case-insensitive)
        email_column = None
        for col in reader.fieldnames or []:
            if col.lower() in ("email", "e-mail", "email address", "subscriber email"):
                email_column = col
                break
        
        if not email_column:
            raise ValueError("No email column found in CSV. Expected column names: 'Email', 'email', 'E-mail', etc.")
        
        email_hashes = []
        for row in reader:
            email = row.get(email_column, "").strip()
            if email:
                try:
                    normalized = normalize_email(email)
                    email_hash = hash_email(normalized)
                    email_hashes.append(email_hash)
                except Exception as e:
                    logger.warning(f"Failed to process email {email}: {e}")
        
        return email_hashes
    
    @app_commands.command(name="import_paid_csv", description="Import paid subscribers from CSV (Admin only)")
    @app_commands.describe(file="CSV file with paid subscriber emails")
    async def import_paid_csv(self, interaction: discord.Interaction, file: discord.Attachment):
        """Import paid subscribers from CSV."""
        if not await self.check_admin_permissions(interaction):
            await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        if not file.filename.endswith(".csv"):
            await interaction.followup.send("❌ Please upload a CSV file.", ephemeral=True)
            return
        
        try:
            # Download file
            csv_bytes = await file.read()
            
            # Parse CSV
            email_hashes = self.parse_csv(csv_bytes)
            
            if not email_hashes:
                await interaction.followup.send("❌ No valid email addresses found in CSV.", ephemeral=True)
                return
            
            # Import to database
            import_stats = await self.db.import_paid_emails(email_hashes)
            
            # Run sync
            sync_stats = await self.sync_premium_roles(interaction.guild)
            
            # Record import
            errors = []
            await self.db.record_import(
                interaction.user.id,
                len(email_hashes),
                import_stats["total_active"],
                sync_stats["granted"],
                sync_stats["revoked"],
                errors
            )
            
            # Log to bot-logs
            log_channel = await self.get_channel(interaction.guild, "bot_logs")
            if log_channel:
                embed = discord.Embed(
                    title="📊 CSV Import Complete",
                    description=(
                        f"**Imported by:** {interaction.user.mention} ({interaction.user})\n"
                        f"**Total rows:** {len(email_hashes)}\n"
                        f"**New emails:** {import_stats['new']}\n"
                        f"**Reactivated:** {import_stats['reactivated']}\n"
                        f"**Total active:** {import_stats['total_active']}\n\n"
                        f"**Sync Results:**\n"
                        f"• Granted Premium: {sync_stats['granted']}\n"
                        f"• Revoked Premium: {sync_stats['revoked']}\n"
                        f"• Skipped: {sync_stats['skipped']}"
                    ),
                    color=discord.Color.green(),
                    timestamp=datetime.now(timezone.utc)
                )
                await log_channel.send(embed=embed)
            
            await interaction.followup.send(
                f"✅ Import complete!\n"
                f"• Processed {len(email_hashes)} emails\n"
                f"• {import_stats['total_active']} active subscribers\n"
                f"• Granted Premium to {sync_stats['granted']} users\n"
                f"• Revoked Premium from {sync_stats['revoked']} users",
                ephemeral=True
            )
            
        except Exception as e:
            logger.error(f"CSV import error: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error importing CSV: {str(e)}", ephemeral=True)
    
    async def sync_premium_roles(self, guild: discord.Guild) -> dict:
        """Sync Premium roles based on paid emails. Returns stats."""
        if not guild:
            return {"granted": 0, "revoked": 0, "skipped": 0}
        
        premium_role = await self.get_role(guild, "premium")
        if not premium_role:
            logger.warning("Premium role not found")
            return {"granted": 0, "revoked": 0, "skipped": 0}
        
        paid_emails = await self.db.get_all_paid_emails()
        paid_set = set(paid_emails)
        
        granted = 0
        revoked = 0
        skipped = 0
        
        # Get all users with verified emails
        async with self.db.get_connection() as db:
            async with db.execute(
                "SELECT discord_user_id, email_hash FROM users WHERE email_verified = TRUE"
            ) as cursor:
                async for row in cursor:
                    user_id = row["discord_user_id"]
                    email_hash = row["email_hash"]
                    
                    if not email_hash:
                        skipped += 1
                        continue
                    
                    member = guild.get_member(user_id)
                    if not member:
                        skipped += 1
                        continue
                    
                    has_premium = premium_role in member.roles
                    is_paid = email_hash in paid_set
                    
                    if is_paid and not has_premium:
                        # Grant Premium
                        try:
                            await member.add_roles(premium_role, reason="Sync: email in paid list")
                            granted += 1
                            logger.info(f"Synced: granted premium to {member}")
                        except Exception as e:
                            logger.error(f"Failed to grant premium to {member}: {e}")
                            skipped += 1
                    elif not is_paid and has_premium and self.config.STRICT_REVOKE:
                        # Revoke Premium
                        try:
                            await member.remove_roles(premium_role, reason="Sync: email not in paid list")
                            revoked += 1
                            logger.info(f"Synced: revoked premium from {member}")
                        except Exception as e:
                            logger.error(f"Failed to revoke premium from {member}: {e}")
                            skipped += 1
                    else:
                        skipped += 1
        
        return {"granted": granted, "revoked": revoked, "skipped": skipped}
    
    @app_commands.command(name="sync_premium", description="Sync Premium roles with paid email list (Admin only)")
    async def sync_premium(self, interaction: discord.Interaction):
        """Sync Premium roles."""
        if not await self.check_admin_permissions(interaction):
            await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("❌ This command can only be used in a server.", ephemeral=True)
            return
        
        stats = await self.sync_premium_roles(guild)
        
        # Log to bot-logs
        log_channel = await self.get_channel(guild, "bot_logs")
        if log_channel:
            embed = discord.Embed(
                title="🔄 Premium Role Sync Complete",
                description=(
                    f"**Synced by:** {interaction.user.mention} ({interaction.user})\n\n"
                    f"**Results:**\n"
                    f"• Granted Premium: {stats['granted']}\n"
                    f"• Revoked Premium: {stats['revoked']}\n"
                    f"• Skipped: {stats['skipped']}"
                ),
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc)
            )
            await log_channel.send(embed=embed)
        
        await interaction.followup.send(
            f"✅ Sync complete!\n"
            f"• Granted Premium: {stats['granted']}\n"
            f"• Revoked Premium: {stats['revoked']}\n"
            f"• Skipped: {stats['skipped']}",
            ephemeral=True
        )
    
    @app_commands.command(name="audit_premium", description="Remove Premium from users not in paid list (Admin only)")
    async def audit_premium(self, interaction: discord.Interaction):
        """Audit and revoke Premium roles."""
        if not await self.check_admin_permissions(interaction):
            await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("❌ This command can only be used in a server.", ephemeral=True)
            return
        
        premium_role = await self.get_role(guild, "premium")
        if not premium_role:
            await interaction.followup.send("❌ Premium role not found.", ephemeral=True)
            return
        
        paid_emails = await self.db.get_all_paid_emails()
        paid_set = set(paid_emails)
        
        revoked = 0
        errors = []
        
        # Check all members with Premium role
        for member in guild.members:
            if premium_role not in member.roles:
                continue
            
            # Check if user has verified email in paid list
            user_data = await self.db.get_user(member.id)
            if not user_data or not user_data.get("email_hash"):
                # No email linked - revoke
                try:
                    await member.remove_roles(premium_role, reason="Audit: no email linked")
                    revoked += 1
                except Exception as e:
                    errors.append(f"{member}: {e}")
                continue
            
            email_hash = user_data["email_hash"]
            if email_hash not in paid_set:
                # Email not in paid list - revoke
                try:
                    await member.remove_roles(premium_role, reason="Audit: email not in paid list")
                    revoked += 1
                except Exception as e:
                    errors.append(f"{member}: {e}")
        
        # Log to bot-logs
        log_channel = await self.get_channel(guild, "bot_logs")
        if log_channel:
            embed = discord.Embed(
                title="🔍 Premium Audit Complete",
                description=(
                    f"**Audited by:** {interaction.user.mention} ({interaction.user})\n\n"
                    f"**Results:**\n"
                    f"• Revoked Premium: {revoked}\n"
                    f"• Errors: {len(errors)}"
                ),
                color=discord.Color.orange(),
                timestamp=datetime.now(timezone.utc)
            )
            if errors:
                embed.add_field(name="Errors", value="\n".join(errors[:10]), inline=False)
            await log_channel.send(embed=embed)
        
        await interaction.followup.send(
            f"✅ Audit complete!\n"
            f"• Revoked Premium: {revoked}\n"
            f"• Errors: {len(errors)}",
            ephemeral=True
        )
    
    @app_commands.command(name="clear_paid_emails", description="Clear all paid emails from database (Admin only)")
    @app_commands.describe(confirm="Must be true to confirm deletion")
    async def clear_paid_emails(self, interaction: discord.Interaction, confirm: bool):
        """Clear all paid emails from database."""
        if not await self.check_admin_permissions(interaction):
            await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        # Require explicit confirmation
        if not confirm:
            await interaction.followup.send(
                "⚠️ **Warning:** This will delete ALL paid emails from the database.\n"
                "Set `confirm=true` to proceed.",
                ephemeral=True
            )
            return
        
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("❌ This command can only be used in a server.", ephemeral=True)
            return
        
        try:
            # Execute DELETE FROM paid_emails
            deleted_count = 0
            if self.db.use_postgres:
                async with self.db.pool.acquire() as conn:
                    # Get count before deletion
                    count_row = await conn.fetchrow("SELECT COUNT(*) as count FROM paid_emails")
                    deleted_count = count_row["count"] if count_row else 0
                    # Execute deletion
                    await conn.execute("DELETE FROM paid_emails")
            else:
                async with self.db.get_connection() as db:
                    # Get count before deletion
                    async with db.execute("SELECT COUNT(*) as count FROM paid_emails") as cursor:
                        count_row = await cursor.fetchone()
                        deleted_count = count_row["count"] if count_row else 0
                    # Execute deletion
                    await db.execute("DELETE FROM paid_emails")
            
            # Log to bot-logs
            log_channel = await self.get_channel(guild, "bot_logs")
            if log_channel:
                embed = discord.Embed(
                    title="🗑️ Paid Emails Cleared",
                    description=(
                        f"**Cleared by:** {interaction.user.mention} ({interaction.user})\n\n"
                        f"**Rows deleted:** {deleted_count}"
                    ),
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc)
                )
                await log_channel.send(embed=embed)
            
            await interaction.followup.send(
                f"✅ Cleared all paid emails from database.\n"
                f"**Rows deleted:** {deleted_count}",
                ephemeral=True
            )
            
        except Exception as e:
            logger.error(f"Error clearing paid emails: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error clearing paid emails: {str(e)}", ephemeral=True)
