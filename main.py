"""
Claude Admin Bot - Discord server audit, moderation, and management bot.
Token is read from Replit Secrets (DISCORD_TOKEN). Never hardcode it.
"""

import os
import asyncio
import datetime
from collections import Counter
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

# ---------------------------------------------------------------------------
# Config (pulled from Replit Secrets)
# ---------------------------------------------------------------------------
TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
OWNER_ID = int(os.environ["OWNER_ID"])

# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------
intents = discord.Intents.all()  # requires Server Members + Message Content intents
bot = commands.Bot(command_prefix="!", intents=intents)

# Per-guild config (in-memory; resets on restart — fine for a starting bot)
guild_config: dict[int, dict] = {}


def cfg(guild_id: int) -> dict:
    if guild_id not in guild_config:
        guild_config[guild_id] = {
            "welcome_channel": None,
            "welcome_message": "Welcome to the server, {member}!",
            "log_channel": None,
            "automod": {
                "spam": False,
                "links": False,
                "mass_mentions": False,
            },
        }
    return guild_config[guild_id]


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id={bot.user.id})")

    # Register/sync commands once per process. on_ready can fire more than once
    # after reconnects, so don't repeatedly overwrite the command tree.
    if getattr(bot, "_commands_synced", False):
        return

    guild = discord.Object(id=GUILD_ID)
    try:
        # Explicitly copy every globally-declared command into the target guild.
        global_commands = bot.tree.get_commands()
        print(f"Global commands loaded: {len(global_commands)}")
        print("Global command names: " + ", ".join(c.name for c in global_commands))

        bot.tree.clear_commands(guild=guild)
        bot.tree.copy_global_to(guild=guild)
        guild_commands = bot.tree.get_commands(guild=guild)
        print(f"Guild commands before sync: {len(guild_commands)}")
        print("Guild command names: " + ", ".join(c.name for c in guild_commands))

        synced = await bot.tree.sync(guild=guild)
        print(f"Synced {len(synced)} commands to guild {GUILD_ID}")
        print("Synced command names: " + ", ".join(c.name for c in synced))

        bot._commands_synced = True
    except Exception as e:
        print(f"Failed to sync commands: {type(e).__name__}: {e}")


@bot.event
async def on_member_join(member: discord.Member):
    c = cfg(member.guild.id)
    # Auto-role: assign the lowest bot-managed role if any role is set as "auto"
    auto_role_id = c.get("auto_role")
    if auto_role_id:
        role = member.guild.get_role(auto_role_id)
        if role:
            try:
                await member.add_roles(role, reason="Auto-role on join")
            except discord.Forbidden:
                pass

    # Welcome message
    if c.get("welcome_channel"):
        channel = member.guild.get_channel(c["welcome_channel"])
        if channel:
            msg = c.get("welcome_message", "Welcome {member}!").format(member=member.mention)
            try:
                await channel.send(msg)
            except discord.Forbidden:
                pass


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    c = cfg(message.guild.id)
    am = c.get("automod", {})

    # Auto-mod: link filtering
    if am.get("links") and ("http://" in message.content or "https://" in message.content):
        if not any(r.permissions.administrator for r in message.author.roles):
            try:
                await message.delete()
                await message.channel.send(
                    f"{message.author.mention} links are not allowed here.", delete_after=5
                )
            except discord.Forbidden:
                pass
            return

    # Auto-mod: mass mentions
    if am.get("mass_mentions") and message.mentions and len(message.mentions) > 5:
        try:
            await message.delete()
        except discord.Forbidden:
            pass
        return

    await bot.process_commands(message)


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    # Role change logging
    if before.roles != after.roles:
        c = cfg(after.guild.id)
        if c.get("log_channel"):
            channel = after.guild.get_channel(c["log_channel"])
            if channel:
                added = [r.name for r in after.roles if r not in before.roles]
                removed = [r.name for r in before.roles if r not in after.roles]
                if added or removed:
                    desc = []
                    if added:
                        desc.append(f"+ {', '.join(added)}")
                    if removed:
                        desc.append(f"- {', '.join(removed)}")
                    embed = discord.Embed(
                        description=f"{after.mention} roles changed\n" + "\n".join(desc),
                        color=discord.Color.blue(),
                    )
                    try:
                        await channel.send(embed=embed)
                    except discord.Forbidden:
                        pass


@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    c = cfg(message.guild.id)
    if not c.get("log_channel"):
        return
    channel = message.guild.get_channel(c["log_channel"])
    if not channel:
        return
    embed = discord.Embed(
        description=f"Message deleted in {message.channel.mention}",
        color=discord.Color.red(),
    )
    embed.add_field(name="Author", value=message.author.mention)
    embed.add_field(name="Content", value=message.content[:1024] or "(empty)", inline=False)
    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        pass


# ---------------------------------------------------------------------------
# Permission helper
# ---------------------------------------------------------------------------
def is_owner():
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user.id == OWNER_ID
    return app_commands.check(predicate)


def perms_summary(perms: discord.Permissions) -> list[str]:
    """Turn a Permissions object into a list of human-readable flags."""
    flags = []
    dangerous = {
        "administrator": perms.administrator,
        "manage_guild": perms.manage_guild,
        "manage_roles": perms.manage_roles,
        "manage_channels": perms.manage_channels,
        "kick_members": perms.kick_members,
        "ban_members": perms.ban_members,
        "mention_everyone": perms.mention_everyone,
        "manage_webhooks": perms.manage_webhooks,
        "manage_messages": perms.manage_messages,
    }
    for name, on in dangerous.items():
        if on:
            flags.append(name)
    return flags


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------
@bot.tree.command(name="ping", description="Check the bot is alive")
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(f"Pong! Latency: {latency}ms")


@bot.tree.command(name="serverinfo", description="Quick server overview")
async def serverinfo(interaction: discord.Interaction):
    g = interaction.guild
    embed = discord.Embed(title=g.name, color=discord.Color.blurple())
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    embed.add_field(name="Members", value=g.member_count)
    embed.add_field(name="Roles", value=len(g.roles))
    embed.add_field(name="Channels", value=len(g.channels))
    embed.add_field(name="Owner", value=(g.owner.mention if g.owner else "Unknown"))
    embed.add_field(name="Created", value=g.created_at.strftime("%Y-%m-%d"))
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="roles", description="List roles and their dangerous permission flags")
@is_owner()
async def roles(interaction: discord.Interaction):
    g = interaction.guild
    lines = []
    for role in sorted(g.roles, key=lambda r: r.position, reverse=True):
        if role.is_default():
            continue
        flags = perms_summary(role.permissions)
        flag_str = ", ".join(flags) if flags else "(none)"
        lines.append(f"`{role.name}` — {flag_str}")
    body = "\n".join(lines) or "No roles found."
    # Split if too long
    chunks = [body[i:i+1900] for i in range(0, len(body), 1900)] or ["No roles found."]
    await interaction.response.send_message(f"**Roles in {g.name}:**\n{chunks[0]}")
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk)


@bot.tree.command(name="channels", description="List channels and their permission overrides")
@is_owner()
async def channels(interaction: discord.Interaction):
    g = interaction.guild
    lines = []
    for ch in g.channels:
        overrides = []
        for target, perms in ch.overwrites.items():
            if isinstance(target, discord.Role):
                allow = perms.pair()[0]
                deny = perms.pair()[1]
                notable = []
                for flag in (allow, deny):
                    if flag.administrator:
                        notable.append("admin")
                    if flag.manage_channels:
                        notable.append("manage-ch")
                    if flag.manage_messages:
                        notable.append("manage-msg")
                if notable:
                    overrides.append(f"{target.name}=[{','.join(notable)}]")
        ov = f" — overrides: {', '.join(overrides)}" if overrides else ""
        lines.append(f"`#{ch.name}` ({ch.type}){ov}")
    body = "\n".join(lines) or "No channels."
    chunks = [body[i:i+1900] for i in range(0, len(body), 1900)] or ["No channels."]
    await interaction.response.send_message(f"**Channels in {g.name}:**\n{chunks[0]}")
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk)


@bot.tree.command(name="members", description="Show member stats and role distribution")
@is_owner()
async def members(interaction: discord.Interaction):
    g = interaction.guild
    humans = sum(1 for m in g.members if not m.bot)
    bots = sum(1 for m in g.members if m.bot)
    online = sum(1 for m in g.members if m.status != discord.Status.offline)

    role_counts = Counter()
    for m in g.members:
        for r in m.roles:
            if not r.is_default():
                role_counts[r.name] += 1

    embed = discord.Embed(title=f"Members in {g.name}", color=discord.Color.green())
    embed.add_field(name="Total", value=g.member_count)
    embed.add_field(name="Humans", value=humans)
    embed.add_field(name="Bots", value=bots)
    embed.add_field(name="Online", value=online)
    if role_counts:
        top = role_counts.most_common(10)
        embed.add_field(
            name="Top roles by member count",
            value="\n".join(f"`{n}` — {c}" for n, c in top),
            inline=False,
        )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="audit", description="Run a full server audit and report issues")
@is_owner()
async def audit(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    g = interaction.guild
    issues = []

    # 1. Roles with admin perms
    admin_roles = [r for r in g.roles if r.permissions.administrator and not r.is_default()]
    if len(admin_roles) > 3:
        issues.append(
            f"⚠️ **{len(admin_roles)} roles have Administrator** — review whether all are needed. "
            f"Roles: {', '.join(r.name for r in admin_roles)}"
        )
    elif admin_roles:
        issues.append(
            f"ℹ️ Administrator roles: {', '.join(r.name for r in admin_roles)}"
        )

    # 2. Roles with dangerous combos
    for r in g.roles:
        if r.is_default():
            continue
        p = r.permissions
        if p.administrator:
            continue
        if p.manage_guild and p.manage_roles and p.manage_channels and p.ban_members:
            issues.append(
                f"⚠️ Role `{r.name}` has manage_guild + manage_roles + manage_channels + ban_members "
                f"— effectively near-admin. Consider splitting responsibilities."
            )
        if p.mention_everyone and not p.administrator:
            issues.append(
                f"⚠️ Role `{r.name}` can mention @everyone without being admin — common abuse vector."
            )

    # 3. @everyone overpermissive
    everyone = g.default_role
    eperm = everyone.permissions
    risky_everyone = []
    if eperm.view_audit_log:
        risky_everyone.append("view_audit_log")
    if eperm.manage_messages:
        risky_everyone.append("manage_messages")
    if eperm.manage_webhooks:
        risky_everyone.append("manage_webhooks")
    if eperm.mention_everyone:
        risky_everyone.append("mention_everyone")
    if risky_everyone:
        issues.append(
            f"⚠️ @everyone has risky permissions: {', '.join(risky_everyone)}"
        )

    # 4. Channel permission overrides
    for ch in g.channels:
        for target, perms in ch.overwrites.items():
            if isinstance(target, discord.Role) and perms.administrator and not target.permissions.administrator:
                issues.append(
                    f"⚠️ Channel `#{ch.name}` grants Administrator to role `{target.name}` "
                    f"whose base role is not admin — could be unintended."
                )

    # 5. No mod log channel
    if not cfg(g.id).get("log_channel"):
        issues.append("ℹ️ No mod log channel set. Use `/log #channel` to enable event logging.")

    # 6. No welcome channel
    if not cfg(g.id).get("welcome_channel"):
        issues.append("ℹ️ No welcome channel set. Use `/welcome #channel` to greet new members.")

    # 7. Member/admin ratio
    admin_members = set()
    for r in admin_roles:
        admin_members.update(r.members)
    if admin_members:
        ratio = len(admin_members) / max(g.member_count, 1)
        if ratio > 0.1 and g.member_count > 20:
            issues.append(
                f"⚠️ {len(admin_members)} admins for {g.member_count} members "
                f"({ratio:.0%}) — high. Consider consolidating."
            )

    # 8. Role hierarchy sanity
    bot_role = g.me.top_role
    admin_role_top = max(admin_roles, key=lambda r: r.position, default=None)
    if admin_role_top and bot_role.position < admin_role_top.position:
        issues.append(
            f"ℹ️ My top role is below `{admin_role_top.name}`. "
            f"I can't moderate that role's members. Move my role up."
        )

    embed = discord.Embed(
        title=f"Audit: {g.name}",
        color=discord.Color.gold() if issues else discord.Color.green(),
    )
    if not issues:
        embed.description = "✅ No major issues found."
    else:
        embed.description = "\n\n".join(issues)
    embed.set_footer(text=f"{g.member_count} members • {len(g.roles)} roles • {len(g.channels)} channels")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="restructure", description="Propose a clean role hierarchy")
@is_owner()
async def restructure(interaction: discord.Interaction):
    g = interaction.guild
    proposal = [
        ("Owner", "You. Full admin. Don't share."),
        ("Head Admin", "Trusted co-owner. Full admin."),
        ("Admin", "All perms minus admin flag. Manages everything."),
        ("Senior Mod", "manage_messages, kick, ban, mute, manage_threads. Cannot manage roles/channels."),
        ("Mod", "manage_messages, kick, mute. Cannot ban or manage roles."),
        ("Helper", "manage_messages only. Tidy-only role."),
        ("Verified", "Auto-assigned after verification gate."),
        ("Member", "Default after join. No special perms."),
        ("Muted", "No send_messages, no speak, no add_reactions. Server-wide override."),
        ("Bot", "Read-only + send in designated bot channels. No kick/ban."),
    ]
    lines = ["**Suggested role hierarchy (top to bottom):**", ""]
    for name, desc in proposal:
        lines.append(f"**{name}** — {desc}")
    lines.append("")
    lines.append("Reply `/applyrestructure` (I'll add a confirm step next) to scaffold, or I can write per-role permission JSON you paste into Discord manually.")
    await interaction.response.send_message("\n".join(lines))


# ---------------------------------------------------------------------------
# Moderation commands
# ---------------------------------------------------------------------------
@bot.tree.command(name="purge", description="Bulk-delete messages (default 10, max 100)")
@app_commands.describe(amount="How many messages to delete (1-100)")
@is_owner()
async def purge(interaction: discord.Interaction, amount: int = 10):
    if amount < 1 or amount > 100:
        await interaction.response.send_message("Amount must be 1-100.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True, ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"Deleted {len(deleted)} messages.", ephemeral=True)


@bot.tree.command(name="kick", description="Kick a member")
@app_commands.describe(member="Who to kick", reason="Why")
@is_owner()
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason given"):
    if member.top_role >= interaction.guild.me.top_role:
        await interaction.response.send_message("That member's role is above mine. Can't kick.", ephemeral=True)
        return
    await member.kick(reason=reason)
    await interaction.response.send_message(f"Kicked {member.mention} — {reason}")


@bot.tree.command(name="ban", description="Ban a member")
@app_commands.describe(member="Who to ban", reason="Why", delete_days="Days of message history to delete (0-7)")
@is_owner()
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason given", delete_days: int = 0):
    if member.top_role >= interaction.guild.me.top_role:
        await interaction.response.send_message("That member's role is above mine. Can't ban.", ephemeral=True)
        return
    await member.ban(reason=reason, delete_message_days=max(0, min(7, delete_days)))
    await interaction.response.send_message(f"Banned {member.mention} — {reason}")


@bot.tree.command(name="mute", description="Timeout a member for N minutes (max 40320 = 28 days)")
@app_commands.describe(member="Who to mute", minutes="Duration in minutes", reason="Why")
@is_owner()
async def mute(interaction: discord.Interaction, member: discord.Member, minutes: int, reason: str = "No reason given"):
    if minutes < 1 or minutes > 40320:
        await interaction.response.send_message("Minutes must be 1-40320.", ephemeral=True)
        return
    duration = datetime.timedelta(minutes=minutes)
    await member.timeout(duration, reason=reason)
    await interaction.response.send_message(f"Timed out {member.mention} for {minutes}m — {reason}")


# ---------------------------------------------------------------------------
# Config commands
# ---------------------------------------------------------------------------
@bot.tree.command(name="welcome", description="Set the welcome channel and message")
@app_commands.describe(channel="Channel to post welcomes in", message="Message (use {member} for the mention)")
@is_owner()
async def welcome(interaction: discord.Interaction, channel: discord.TextChannel, message: str = "Welcome to the server, {member}!"):
    c = cfg(interaction.guild.id)
    c["welcome_channel"] = channel.id
    c["welcome_message"] = message
    await interaction.response.send_message(
        f"Welcome channel set to {channel.mention}. Message: `{message}`",
        ephemeral=True,
    )


@bot.tree.command(name="log", description="Set the mod log channel")
@app_commands.describe(channel="Channel to log events in (or 'off' to disable)")
@is_owner()
async def log(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    c = cfg(interaction.guild.id)
    if channel is None:
        c["log_channel"] = None
        await interaction.response.send_message("Logging disabled.", ephemeral=True)
    else:
        c["log_channel"] = channel.id
        await interaction.response.send_message(f"Logging to {channel.mention}.", ephemeral=True)


@bot.tree.command(name="automod", description="Toggle auto-mod rules")
@app_commands.describe(rule="Which rule to toggle", enabled="True to enable, False to disable")
@app_commands.choices(rule=[
    app_commands.Choice(name="spam (basic)", value="spam"),
    app_commands.Choice(name="links (delete http/https)", value="links"),
    app_commands.Choice(name="mass_mentions (>5 in one msg)", value="mass_mentions"),
])
@is_owner()
async def automod(interaction: discord.Interaction, rule: str, enabled: bool):
    c = cfg(interaction.guild.id)
    c.setdefault("automod", {})
    c["automod"][rule] = enabled
    state = "ON" if enabled else "OFF"
    await interaction.response.send_message(f"Auto-mod `{rule}` is now **{state}**.", ephemeral=True)


@bot.tree.command(name="autorole", description="Set the role auto-assigned to new members")
@app_commands.describe(role="Role to auto-assign (or 'off' to disable)")
@is_owner()
async def autorole(interaction: discord.Interaction, role: Optional[discord.Role] = None):
    c = cfg(interaction.guild.id)
    if role is None:
        c["auto_role"] = None
        await interaction.response.send_message("Auto-role disabled.", ephemeral=True)
    else:
        if role >= interaction.guild.me.top_role:
            await interaction.response.send_message("That role is above mine. I can't assign it.", ephemeral=True)
            return
        c["auto_role"] = role.id
        await interaction.response.send_message(f"Auto-role set to {role.mention}.", ephemeral=True)


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
    else:
        print(f"Command error: {error}")
        try:
            await interaction.response.send_message(f"Error: {error}", ephemeral=True)
        except discord.InteractionResponded:
            await interaction.followup.send(f"Error: {error}", ephemeral=True)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    bot.run(TOKEN)
