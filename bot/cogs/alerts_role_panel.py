"""Alerts role self-serve opt-in/out via slash command + persistent button panel."""
import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)


PANEL_CUSTOM_ID_OPTIN = "alerts_role_panel:optin"
PANEL_CUSTOM_ID_OPTOUT = "alerts_role_panel:optout"
PANEL_CUSTOM_ID_STATUS = "alerts_role_panel:status"


async def _log_to_bot_logs(bot, guild: discord.Guild, title: str, description: str, color: discord.Color) -> None:
    """Best-effort log to #bot-logs."""
    try:
        admin_cog = bot.get_cog("AdminRolesCog")
        log_channel = await admin_cog.get_channel(guild, "bot_logs") if admin_cog else None
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


class AlertsRolePanelView(discord.ui.View):
    """Persistent panel for Alerts role opt-in/out."""

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    async def _get_panel_channel_id(self, guild: discord.Guild) -> Optional[int]:
        cfg = getattr(self.bot, "config", None)
        if not cfg:
            return None

        channel_id = getattr(cfg, "ALERTS_ROLE_PANEL_CHANNEL_ID", None)
        if channel_id:
            return int(channel_id)

        channel_name = (getattr(cfg, "ALERTS_ROLE_PANEL_CHANNEL_NAME", None) or "").strip()
        if channel_name:
            ch = discord.utils.get(guild.text_channels, name=channel_name)
            return ch.id if ch else None
        return None

    async def _enforce_panel_channel(self, interaction: discord.Interaction) -> bool:
        """Optional strictness: require button interactions happen in configured panel channel."""
        if not interaction.guild:
            await interaction.response.send_message("❌ This can only be used in a server.", ephemeral=True)
            return False

        panel_channel_id = await self._get_panel_channel_id(interaction.guild)
        if panel_channel_id and interaction.channel_id != panel_channel_id:
            await interaction.response.send_message(
                f"❌ Please use this in <#{panel_channel_id}>.",
                ephemeral=True,
            )
            return False
        return True

    async def _resolve_alerts_role(self, guild: discord.Guild) -> Optional[discord.Role]:
        cfg = getattr(self.bot, "config", None)
        if not cfg:
            return None

        role_id = getattr(cfg, "ALERTS_ROLE_ID", None)
        if role_id:
            role = guild.get_role(int(role_id))
            if role:
                return role

        role_name = (getattr(cfg, "ALERTS_ROLE_NAME", None) or "Alerts").strip()
        role = discord.utils.get(guild.roles, name=role_name)
        if not role:
            rn = role_name.casefold()
            role = next((r for r in guild.roles if r.name.casefold() == rn), None)
        return role

    async def _member_from_interaction(self, interaction: discord.Interaction) -> Optional[discord.Member]:
        if not interaction.guild:
            return None
        m = interaction.guild.get_member(interaction.user.id)
        if m:
            return m
        try:
            return await interaction.guild.fetch_member(interaction.user.id)
        except Exception:
            return None

    async def _handle_role_change(
        self,
        interaction: discord.Interaction,
        action: str,
    ) -> None:
        if not await self._enforce_panel_channel(interaction):
            return

        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if not guild:
            await interaction.followup.send("❌ This can only be used in a server.", ephemeral=True)
            return

        role = await self._resolve_alerts_role(guild)
        if not role:
            await _log_to_bot_logs(
                self.bot,
                guild,
                title="❌ Alerts Role Panel: Role Not Found",
                description=(
                    f"**User:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                    f"**Reason:** Alerts role not found. Set `ALERTS_ROLE_ID` (preferred) or `ALERTS_ROLE_NAME`."
                ),
                color=discord.Color.red(),
            )
            await interaction.followup.send(
                "❌ Alerts role not found. Please contact an admin to configure `ALERTS_ROLE_ID`/`ALERTS_ROLE_NAME`.",
                ephemeral=True,
            )
            return

        member = await self._member_from_interaction(interaction)
        if not member:
            await interaction.followup.send("❌ Could not load your member record.", ephemeral=True)
            return

        try:
            if action == "optin":
                if role in member.roles:
                    await interaction.followup.send("✅ You’re already opted in to Alerts.", ephemeral=True)
                    return
                await member.add_roles(role, reason="Self-serve: Alerts opt-in")
                await interaction.followup.send("✅ You’re now opted in to Alerts.", ephemeral=True)
                return

            if action == "optout":
                if role not in member.roles:
                    await interaction.followup.send("✅ You’re already opted out of Alerts.", ephemeral=True)
                    return
                await member.remove_roles(role, reason="Self-serve: Alerts opt-out")
                await interaction.followup.send("✅ You’re now opted out of Alerts.", ephemeral=True)
                return

            if action == "status":
                has = role in member.roles
                await interaction.followup.send(
                    f"🔔 Alerts role: **{'ON' if has else 'OFF'}**",
                    ephemeral=True,
                )
                return

            await interaction.followup.send("❌ Unknown action.", ephemeral=True)

        except discord.Forbidden:
            await _log_to_bot_logs(
                self.bot,
                guild,
                title="❌ Alerts Role Panel: Role Update Forbidden",
                description=(
                    f"**User:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                    f"**Role:** {role.mention} (`{role.id}`)\n"
                    f"**Action:** `{action}`\n\n"
                    "Bot lacks permission or role hierarchy is incorrect. Ensure bot has **Manage Roles** and its top role is above **Alerts**."
                ),
                color=discord.Color.red(),
            )
            await interaction.followup.send(
                "❌ I can’t update roles due to permissions/role hierarchy.\n"
                "Ask an admin to ensure I have **Manage Roles** and my top role is above the **Alerts** role.",
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"Alerts role panel error: {e}", exc_info=True)
            await interaction.followup.send("❌ An error occurred. Please try again later.", ephemeral=True)

    @discord.ui.button(label="🔔 Opt-in to Alerts", style=discord.ButtonStyle.success, custom_id=PANEL_CUSTOM_ID_OPTIN)
    async def optin(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_role_change(interaction, "optin")

    @discord.ui.button(label="🔕 Opt-out of Alerts", style=discord.ButtonStyle.secondary, custom_id=PANEL_CUSTOM_ID_OPTOUT)
    async def optout(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_role_change(interaction, "optout")

    @discord.ui.button(label="🔄 Check status", style=discord.ButtonStyle.primary, custom_id=PANEL_CUSTOM_ID_STATUS)
    async def status(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_role_change(interaction, "status")


class AlertsRolePanelCog(commands.Cog):
    """Self-serve Alerts role opt-in/out."""

    def __init__(self, bot):
        self.bot = bot
        self.config = bot.config
        self.db = bot.db

    async def _resolve_alerts_role(self, guild: discord.Guild) -> Optional[discord.Role]:
        role_id = getattr(self.config, "ALERTS_ROLE_ID", None)
        if role_id:
            role = guild.get_role(int(role_id))
            if role:
                return role

        role_name = (getattr(self.config, "ALERTS_ROLE_NAME", None) or "Alerts").strip()
        role = discord.utils.get(guild.roles, name=role_name)
        if not role:
            rn = role_name.casefold()
            role = next((r for r in guild.roles if r.name.casefold() == rn), None)
        return role

    async def _resolve_panel_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        cid = getattr(self.config, "ALERTS_ROLE_PANEL_CHANNEL_ID", None)
        if cid:
            try:
                ch = guild.get_channel(int(cid))
            except Exception:
                ch = None
            # Must be messageable (TextChannel/AnnouncementChannel are TextChannel in discord.py)
            if ch is not None and hasattr(ch, "send") and isinstance(ch, discord.abc.GuildChannel):
                if isinstance(ch, discord.TextChannel):
                    return ch
                # Not a text channel (could be category/forum/etc.)
                return None

        name = (getattr(self.config, "ALERTS_ROLE_PANEL_CHANNEL_NAME", None) or "").strip()
        if name:
            return discord.utils.get(guild.text_channels, name=name)
        return None

    async def _log(self, guild: discord.Guild, title: str, description: str, color: discord.Color) -> None:
        await _log_to_bot_logs(self.bot, guild, title, description, color)

    def _guild_only(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            return False
        if self.config.GUILD_ID and interaction.guild.id != self.config.GUILD_ID:
            return False
        return True

    @app_commands.command(name="alerts_toggle", description="Toggle opt-in/out for Alerts pings")
    async def alerts_toggle(self, interaction: discord.Interaction):
        """Toggle the Alerts role for the user."""
        if not self._guild_only(interaction):
            await interaction.response.send_message("❌ This command can only be used in the server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if not guild:
            await interaction.followup.send("❌ This command can only be used in a server.", ephemeral=True)
            return

        role = await self._resolve_alerts_role(guild)
        if not role:
            await self._log(
                guild,
                title="❌ Alerts Toggle: Role Not Found",
                description="Alerts role not found. Configure `ALERTS_ROLE_ID` (preferred) or `ALERTS_ROLE_NAME`.",
                color=discord.Color.red(),
            )
            await interaction.followup.send(
                "❌ Alerts role not found. Please contact an admin to configure it.",
                ephemeral=True,
            )
            return

        member = guild.get_member(interaction.user.id)
        if not member:
            try:
                member = await guild.fetch_member(interaction.user.id)
            except Exception:
                member = None
        if not member:
            await interaction.followup.send("❌ Could not load your member record.", ephemeral=True)
            return

        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Self-serve: Alerts toggle off")
                await interaction.followup.send("🔕 Alerts: **OFF**", ephemeral=True)
            else:
                await member.add_roles(role, reason="Self-serve: Alerts toggle on")
                await interaction.followup.send("🔔 Alerts: **ON**", ephemeral=True)
        except discord.Forbidden:
            await self._log(
                guild,
                title="❌ Alerts Toggle: Forbidden",
                description=(
                    f"Failed to update Alerts role for {interaction.user.mention} (`{interaction.user.id}`).\n\n"
                    "Ensure bot has **Manage Roles** and its top role is above the **Alerts** role."
                ),
                color=discord.Color.red(),
            )
            await interaction.followup.send(
                "❌ I can’t update roles due to permissions/role hierarchy. Please contact an admin.",
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"alerts_toggle error: {e}", exc_info=True)
            await interaction.followup.send("❌ An error occurred. Please try again later.", ephemeral=True)

    @app_commands.command(name="post_alerts_role_panel", description="Post or update the Alerts role opt-in panel (Admin only)")
    async def post_alerts_role_panel(self, interaction: discord.Interaction):
        """Post/refresh the persistent alerts role panel message."""
        # Admin-only: reuse existing mod/admin checks
        admin_cog = self.bot.get_cog("AdminRolesCog")
        if not admin_cog or not await admin_cog.check_mod_permissions(interaction):
            await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
            return

        if not self._guild_only(interaction):
            await interaction.response.send_message("❌ This command can only be used in the server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if not guild:
            await interaction.followup.send("❌ This command can only be used in a server.", ephemeral=True)
            return

        cfg_id = getattr(self.config, "ALERTS_ROLE_PANEL_CHANNEL_ID", None)
        cfg_name = getattr(self.config, "ALERTS_ROLE_PANEL_CHANNEL_NAME", None)
        panel_channel = await self._resolve_panel_channel(guild)
        if not panel_channel:
            extra = ""
            if cfg_id:
                try:
                    raw = guild.get_channel(int(cfg_id))
                except Exception:
                    raw = None
                if raw is None:
                    extra = f"\nConfigured `ALERTS_ROLE_PANEL_CHANNEL_ID={cfg_id}` but no channel with that ID exists in this guild."
                else:
                    extra = (
                        f"\nConfigured `ALERTS_ROLE_PANEL_CHANNEL_ID={cfg_id}` but it is a `{type(raw).__name__}` "
                        "and I can only post the panel to a text channel."
                    )
            else:
                extra = f"\nConfigured channel name fallback: `{cfg_name or 'alerts-settings'}`"

            await self._log(
                guild,
                title="❌ Alerts Role Panel: Panel Channel Not Found",
                description=(
                    f"**Configured ID:** `{cfg_id}`\n"
                    f"**Configured Name:** `{cfg_name}`\n"
                    f"**Invoked In:** <#{interaction.channel_id}> (`{interaction.channel_id}`)\n"
                    f"**Guild:** `{guild.id}`"
                ),
                color=discord.Color.red(),
            )

            await interaction.followup.send(
                "❌ Panel channel not found. Set `ALERTS_ROLE_PANEL_CHANNEL_ID` to a **text channel** ID."
                + extra,
                ephemeral=True,
            )
            return

        # Restrict posting location (must run in configured channel)
        if interaction.channel_id != panel_channel.id:
            await interaction.followup.send(
                f"❌ Please run this command in {panel_channel.mention}.",
                ephemeral=True,
            )
            return

        role = await self._resolve_alerts_role(guild)
        if not role:
            await self._log(
                guild,
                title="❌ Alerts Role Panel: Role Not Found",
                description="Alerts role not found. Configure `ALERTS_ROLE_ID` (preferred) or `ALERTS_ROLE_NAME`.",
                color=discord.Color.red(),
            )
            await interaction.followup.send(
                "❌ Alerts role not found. Please configure `ALERTS_ROLE_ID`/`ALERTS_ROLE_NAME` first.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="🔔 Alerts Role Settings",
            description=(
                "Use the buttons below to opt in/out of the **Alerts** role.\n\n"
                "If you opt in, you’ll be @mentioned when alerts are posted."
            ),
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Role", value=f"{role.mention}", inline=False)
        embed.set_footer(text="Hidden Gems Research - The Gem Vault")

        view = AlertsRolePanelView(self.bot)

        try:
            existing = await self.db.get_alerts_role_panel(guild.id)
        except Exception:
            existing = None

        if existing and existing.get("message_id") and int(existing.get("channel_id", 0)) == panel_channel.id:
            try:
                msg = await panel_channel.fetch_message(int(existing["message_id"]))
                await msg.edit(embed=embed, view=view)
                await self.db.set_alerts_role_panel(guild.id, panel_channel.id, msg.id)
                await self._log(
                    guild,
                    title="✅ Alerts Role Panel Updated",
                    description=f"Panel refreshed in {panel_channel.mention} (`{panel_channel.id}`) message `{msg.id}`.",
                    color=discord.Color.green(),
                )
                await interaction.followup.send(f"✅ Alerts role panel updated in {panel_channel.mention}.", ephemeral=True)
                return
            except discord.NotFound:
                pass
            except discord.Forbidden:
                await self._log(
                    guild,
                    title="❌ Alerts Role Panel Update Forbidden",
                    description=f"Missing permission to edit messages in {panel_channel.mention}.",
                    color=discord.Color.red(),
                )
                await interaction.followup.send("❌ I can’t edit messages in that channel.", ephemeral=True)
                return
            except Exception as e:
                logger.warning(f"Failed to edit existing alerts role panel: {e}")

        # Post new panel
        try:
            msg = await panel_channel.send(embed=embed, view=view)
            await self.db.set_alerts_role_panel(guild.id, panel_channel.id, msg.id)
            await self._log(
                guild,
                title="✅ Alerts Role Panel Posted",
                description=f"Panel posted in {panel_channel.mention} (`{panel_channel.id}`) message `{msg.id}`.",
                color=discord.Color.green(),
            )
            await interaction.followup.send(f"✅ Alerts role panel posted in {panel_channel.mention}.", ephemeral=True)
        except discord.Forbidden:
            await self._log(
                guild,
                title="❌ Alerts Role Panel Post Forbidden",
                description=f"Missing permission to send messages in {panel_channel.mention}.",
                color=discord.Color.red(),
            )
            await interaction.followup.send("❌ I can’t send messages in that channel.", ephemeral=True)
        except Exception as e:
            logger.error(f"Error posting alerts role panel: {e}", exc_info=True)
            await interaction.followup.send("❌ Error posting panel. Check bot logs.", ephemeral=True)

