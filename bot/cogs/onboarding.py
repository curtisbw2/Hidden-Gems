"""Onboarding cog: welcome messages, free role assignment, /start command."""
import logging
import difflib
import re
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _normalize_name(s: str) -> str:
    """Normalize a name for fuzzy equality checks (lowercase, alnum-only)."""
    s = (s or "").strip().casefold()
    return _NON_ALNUM_RE.sub("", s)


def _split_csv(s: str) -> list[str]:
    return [p.strip() for p in (s or "").split(",") if p.strip()]


def _parse_int_set(csv: str) -> set[int]:
    out: set[int] = set()
    for p in _split_csv(csv):
        try:
            out.add(int(p))
        except ValueError:
            continue
    return out


def _avatar_fingerprint(user: discord.abc.User) -> str | None:
    """Best-effort stable-ish avatar fingerprint (prefers key, falls back to URL)."""
    try:
        asset = getattr(user, "display_avatar", None)
        if not asset:
            return None
        key = getattr(asset, "key", None)
        if key:
            return str(key)
        url = getattr(asset, "url", None)
        return str(url) if url else None
    except Exception:
        return None


def _best_name_similarity(owner_aliases: set[str], member_names: list[str]) -> float:
    """
    Return the best similarity ratio between owner aliases and member names.
    Expects both sets/lists to contain BOTH raw and normalized variants.
    """
    # Prefer normalized variants (more stable), but we don't strictly separate here—caller includes both.
    best = 0.0
    for oa in owner_aliases:
        if not oa:
            continue
        for mn in member_names:
            if not mn:
                continue
            if oa == mn:
                return 1.0
            # Avoid super-short fuzz comparisons producing noisy matches
            if len(oa) < 3 or len(mn) < 3:
                continue
            r = difflib.SequenceMatcher(None, oa, mn).ratio()
            if r > best:
                best = r
    return best


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


async def _announce_imposter_ejected(bot, guild: discord.Guild, member: discord.Member) -> None:
    """Best-effort announce to a configured channel when an imposter is banned."""
    try:
        cfg = getattr(bot, "config", None)
        if not cfg:
            return
        channel_id = getattr(cfg, "SECURITY_IMPOSTER_ANNOUNCE_CHANNEL_ID", None)
        if not channel_id:
            return
        ch = guild.get_channel(int(channel_id))
        if not ch or not isinstance(ch, discord.TextChannel):
            return
        await ch.send(f"Imposter ejected {member.mention}")
    except Exception:
        pass


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

    async def get_quarantine_role(self, guild: discord.Guild) -> discord.Role | None:
        """Resolve quarantine role by ID or name."""
        role_id = getattr(self.config, "SECURITY_QUARANTINE_ROLE_ID", None)
        if role_id:
            role = guild.get_role(int(role_id))
            if role:
                return role
        role_name = (getattr(self.config, "SECURITY_QUARANTINE_ROLE_NAME", None) or "").strip()
        if role_name:
            role = discord.utils.get(guild.roles, name=role_name)
            if role:
                return role
            rn = role_name.casefold()
            role = next((r for r in guild.roles if r.name.casefold() == rn), None)
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

    async def _resolve_owner_user(self, guild: discord.Guild) -> discord.Member | None:
        owner_id = getattr(self.config, "SECURITY_OWNER_USER_ID", None)
        if not owner_id:
            return None
        m = guild.get_member(int(owner_id))
        if m:
            return m
        try:
            return await guild.fetch_member(int(owner_id))
        except Exception:
            return None

    def _get_owner_aliases(self, owner_member: discord.Member | None) -> set[str]:
        aliases: set[str] = set()

        cfg_aliases = _split_csv(getattr(self.config, "SECURITY_OWNER_NAME_ALIASES", ""))
        for a in cfg_aliases:
            aliases.add(a)

        if owner_member:
            aliases.add(owner_member.display_name)
            # Under discord.py, Member is also a User; "name" is the username.
            aliases.add(getattr(owner_member, "name", "") or "")
            aliases.add(getattr(owner_member, "global_name", "") or "")

        # Keep raw + normalized variants for easy matching
        expanded: set[str] = set()
        for a in aliases:
            if not a:
                continue
            expanded.add(a)
            expanded.add(_normalize_name(a))
        return expanded

    def _member_name_candidates(self, member: discord.Member) -> list[str]:
        vals: list[str] = []
        vals.append(member.display_name or "")
        vals.append(getattr(member, "name", "") or "")
        vals.append(getattr(member, "global_name", "") or "")
        # Also include normalized variants
        out: list[str] = []
        for v in vals:
            if v:
                out.append(v)
                out.append(_normalize_name(v))
        return out

    async def _apply_security_action(self, member: discord.Member, reason: str) -> bool:
        """Return True if we took an enforcement action (ban/kick/quarantine)."""
        action = (getattr(self.config, "SECURITY_IMPERSONATION_ACTION", "ban") or "ban").strip().lower()
        guild = member.guild

        if action == "quarantine":
            qrole = await self.get_quarantine_role(guild)
            if not qrole:
                await _log_to_bot_logs(
                    self.bot,
                    guild,
                    title="❌ Security: Quarantine Role Missing",
                    description=(
                        f"**User:** {member.mention} (`{member.id}`)\n"
                        f"**Reason:** {reason}\n"
                        f"**Action:** quarantine requested but role not found. Set `SECURITY_QUARANTINE_ROLE_ID`/`SECURITY_QUARANTINE_ROLE_NAME`."
                    ),
                    color=discord.Color.red(),
                )
                return False
            try:
                await member.add_roles(qrole, reason=f"Security quarantine: {reason}")
                return True
            except discord.Forbidden:
                await _log_to_bot_logs(
                    self.bot,
                    guild,
                    title="❌ Security: Quarantine Failed (Forbidden)",
                    description=(
                        f"**User:** {member.mention} (`{member.id}`)\n"
                        f"**Reason:** {reason}\n"
                        f"**Action:** quarantine\n"
                        f"**Hint:** Ensure the bot has **Manage Roles** and is above the quarantine role."
                    ),
                    color=discord.Color.red(),
                )
                return False
            except Exception as e:
                logger.error(f"Security quarantine error for {member}: {e}", exc_info=True)
                return False

        if action == "kick":
            try:
                await member.kick(reason=f"Security kick: {reason}")
                return True
            except discord.Forbidden:
                await _log_to_bot_logs(
                    self.bot,
                    guild,
                    title="❌ Security: Kick Failed (Forbidden)",
                    description=(
                        f"**User:** {member.mention} (`{member.id}`)\n"
                        f"**Reason:** {reason}\n"
                        f"**Action:** kick\n"
                        f"**Hint:** Ensure the bot has **Kick Members** permission."
                    ),
                    color=discord.Color.red(),
                )
                return False
            except Exception as e:
                logger.error(f"Security kick error for {member}: {e}", exc_info=True)
                return False

        # Default: ban
        try:
            await guild.ban(member, reason=f"Security ban: {reason}", delete_message_days=0)
            return True
        except TypeError:
            # discord.py versions may not accept delete_message_days for guild.ban
            try:
                await guild.ban(member, reason=f"Security ban: {reason}")
                return True
            except Exception as e:
                logger.error(f"Security ban error for {member}: {e}", exc_info=True)
                return False
        except discord.Forbidden:
            await _log_to_bot_logs(
                self.bot,
                guild,
                title="❌ Security: Ban Failed (Forbidden)",
                description=(
                    f"**User:** {member.mention} (`{member.id}`)\n"
                    f"**Reason:** {reason}\n"
                    f"**Action:** ban\n"
                    f"**Hint:** Ensure the bot has **Ban Members** permission."
                ),
                color=discord.Color.red(),
            )
            return False
        except Exception as e:
            logger.error(f"Security ban error for {member}: {e}", exc_info=True)
            return False

    async def _security_guard(self, member: discord.Member, *, event: str, check_untrusted_bots: bool) -> bool:
        """
        Security guard. Returns True if we took an enforcement action (ban/kick/quarantine).
        """
        if not getattr(self.config, "SECURITY_IMPERSONATION_GUARD_ENABLED", True):
            return False

        guild = member.guild

        trusted_bot_ids = _parse_int_set(getattr(self.config, "SECURITY_TRUSTED_BOT_IDS", ""))
        if check_untrusted_bots and member.bot and getattr(self.config, "SECURITY_BAN_UNTRUSTED_BOTS_ON_JOIN", False):
            if member.id not in trusted_bot_ids:
                reason = f"Untrusted bot ({event}) (not in SECURITY_TRUSTED_BOT_IDS)"
                action = (getattr(self.config, "SECURITY_IMPERSONATION_ACTION", "ban") or "ban").strip().lower()
                took = await self._apply_security_action(member, reason=reason)
                if took:
                    await _log_to_bot_logs(
                        self.bot,
                        guild,
                        title="🛡️ Security: Untrusted Bot Blocked",
                        description=(
                            f"**Bot:** {member} (`{member.id}`)\n"
                            f"**Reason:** {reason}\n"
                            f"**Action:** {getattr(self.config, 'SECURITY_IMPERSONATION_ACTION', 'ban')}"
                        ),
                        color=discord.Color.orange(),
                    )
                    if action == "ban":
                        await _announce_imposter_ejected(self.bot, guild, member)
                return took

        owner_id = getattr(self.config, "SECURITY_OWNER_USER_ID", None)
        if owner_id and member.id == int(owner_id):
            return False  # never block the real owner

        owner_member = await self._resolve_owner_user(guild)
        if not owner_member:
            # Without owner reference, we can still use explicit aliases, but avatar matching won't work.
            pass

        owner_aliases = self._get_owner_aliases(owner_member)
        member_names = self._member_name_candidates(member)

        best_sim = _best_name_similarity(owner_aliases, member_names)
        threshold = float(getattr(self.config, "SECURITY_NAME_SIMILARITY_THRESHOLD", 0.84) or 0.84)
        fuzzy_name_match = best_sim >= max(0.0, min(1.0, threshold))

        avatar_match = False
        if owner_member:
            owner_fp = _avatar_fingerprint(owner_member)
            member_fp = _avatar_fingerprint(member)
            avatar_match = bool(owner_fp and member_fp and owner_fp == member_fp)

        # Supporting signal: very new accounts are far more likely to be mass-created impersonators
        min_days = int(getattr(self.config, "SECURITY_MIN_ACCOUNT_AGE_DAYS", 3) or 3)
        try:
            created_at = member.created_at
            young_account = bool(created_at and (discord.utils.utcnow() - created_at) < timedelta(days=min_days))
        except Exception:
            young_account = False

        # Core rule (tailored for your scam pattern, still false-positive aware):
        # - Require recent account age, AND
        # - Prefer avatar match (strongest), AND
        # - Fuzzy name similarity (handles slight variations)
        #
        # If owner member can't be resolved, we fall back to fuzzy name + young account only.
        triggers_impersonation = False
        if young_account and avatar_match and fuzzy_name_match:
            triggers_impersonation = True
        elif (not owner_member) and young_account and fuzzy_name_match:
            triggers_impersonation = True

        if triggers_impersonation:
            pieces = []
            pieces.append(f"name~{best_sim:.2f}>= {threshold:.2f}")
            if avatar_match:
                pieces.append("avatar-match")
            if young_account:
                pieces.append(f"new-account(<{min_days}d)")
            reason = f"Potential owner impersonation ({event}) ({', '.join(pieces)})"

            action = (getattr(self.config, "SECURITY_IMPERSONATION_ACTION", "ban") or "ban").strip().lower()
            took = await self._apply_security_action(member, reason=reason)
            if took:
                await _log_to_bot_logs(
                    self.bot,
                    guild,
                    title="🛡️ Security: Impersonation Blocked",
                    description=(
                        f"**User:** {member.mention} (`{member.id}`)\n"
                        f"**Reason:** {reason}\n"
                        f"**Owner Reference:** `{getattr(self.config, 'SECURITY_OWNER_USER_ID', None)}`\n"
                        f"**Action:** {getattr(self.config, 'SECURITY_IMPERSONATION_ACTION', 'ban')}"
                    ),
                    color=discord.Color.red(),
                )
                if action == "ban":
                    await _announce_imposter_ejected(self.bot, guild, member)
            return took

        return False

    async def _security_guard_on_join(self, member: discord.Member) -> bool:
        """Join-time guard wrapper (also handles optional untrusted-bot enforcement)."""
        return await self._security_guard(member, event="join", check_untrusted_bots=True)
    
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Handle new member join."""
        guild = member.guild

        # Join-time security guard (optional)
        try:
            handled = await self._security_guard_on_join(member)
            if handled:
                return
        except Exception as e:
            logger.error(f"Security guard error on join for {member}: {e}", exc_info=True)
        
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

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """Enforce impersonation bans on in-guild profile changes (nickname/role avatar)."""
        if not getattr(self.config, "SECURITY_ENFORCE_ON_PROFILE_CHANGE", True):
            return

        # Only run when something profile-like changed
        try:
            name_changed = _normalize_name(before.display_name) != _normalize_name(after.display_name)
        except Exception:
            name_changed = False
        try:
            avatar_changed = _avatar_fingerprint(before) != _avatar_fingerprint(after)
        except Exception:
            avatar_changed = False

        if not (name_changed or avatar_changed):
            return

        try:
            await self._security_guard(after, event="member_update", check_untrusted_bots=False)
        except Exception as e:
            logger.error(f"Security guard error on member_update for {after}: {e}", exc_info=True)

    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User):
        """Enforce impersonation bans on global user profile changes (username/avatar)."""
        if not getattr(self.config, "SECURITY_ENFORCE_ON_PROFILE_CHANGE", True):
            return

        # Fast reject if nothing relevant changed at the user level
        try:
            name_changed = (before.name != after.name) or ((before.global_name or "") != (after.global_name or ""))
        except Exception:
            name_changed = True
        try:
            avatar_changed = _avatar_fingerprint(before) != _avatar_fingerprint(after)
        except Exception:
            avatar_changed = True

        if not (name_changed or avatar_changed):
            return

        # Apply to any guilds we share with this user (optionally narrowed by GUILD_ID)
        guild_id = getattr(self.config, "GUILD_ID", None)
        guilds = [g for g in self.bot.guilds if (not guild_id or g.id == int(guild_id))]

        for g in guilds:
            m = g.get_member(after.id)
            if not m:
                continue
            try:
                await self._security_guard(m, event="user_update", check_untrusted_bots=False)
            except Exception as e:
                logger.error(f"Security guard error on user_update for {m} in {g.id}: {e}", exc_info=True)
    
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
