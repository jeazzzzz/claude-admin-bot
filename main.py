"""
Claude Admin Bot - conversational, mention-driven, server-aware.
Uses OpenRouter (free Llama 3.3 70B) as the brain.
"""

import os
import sys
import json
import asyncio
import traceback
import urllib.request
import urllib.error
from typing import Optional

import discord
from discord.ext import commands

print("[BOOT] Starting Claude Admin Bot", flush=True)
print(f"[BOOT] Python {sys.version.split()[0]}", flush=True)
print(f"[BOOT] discord.py {discord.__version__}", flush=True)

# --- Secrets ---
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID_STR = os.environ.get("GUILD_ID")
OWNER_ID_STR = os.environ.get("OWNER_ID")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")

for name, val in [
    ("DISCORD_TOKEN", DISCORD_TOKEN),
    ("GUILD_ID", GUILD_ID_STR),
    ("OWNER_ID", OWNER_ID_STR),
    ("OPENROUTER_API_KEY", OPENROUTER_API_KEY),
]:
    if not val:
        print(f"[FATAL] {name} is missing!", flush=True)
        sys.exit(1)

GUILD_ID = int(GUILD_ID_STR)
OWNER_ID = int(OWNER_ID_STR)

print(f"[BOOT] Guild={GUILD_ID} Owner={OWNER_ID} ORKey={'set' if OPENROUTER_API_KEY else 'MISSING'}", flush=True)

# --- Bot ---
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

conversation_memory: dict[int, list[dict]] = {}


# =============================================================================
# OPENROUTER API CALL
# =============================================================================
# OpenRouter is OpenAI-compatible. We use a free Llama 3.3 70B model.
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "meta-llama/llama-3.3-70b-instruct:free"


def call_openrouter(system_prompt: str, history: list[dict], user_msg: str, max_tokens: int = 1500) -> str:
    """Synchronous OpenRouter call. Returns assistant text."""
    messages = [{"role": "system", "content": system_prompt}]
    for msg in history:
        messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": user_msg})

    payload = {
        "model": OPENROUTER_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.4,
    }

    req = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/jeazzzzz/claude-admin-bot",
            "X-Title": "Claude Admin Bot",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError) as e:
        print(f"[OR] Unexpected response: {json.dumps(data)[:500]}", flush=True)
        raise RuntimeError(f"Bad OpenRouter response: {e}")


# =============================================================================
# SERVER SNAPSHOT
# =============================================================================
def snapshot_server(guild: discord.Guild) -> str:
    """Compact text view of the server for the LLM."""
    roles = []
    for r in sorted(guild.roles, key=lambda x: x.position, reverse=True):
        if r.is_default():
            continue
        flags = [n for n, v in {
            "administrator": r.permissions.administrator,
            "manage_guild": r.permissions.manage_guild,
            "manage_roles": r.permissions.manage_roles,
            "manage_channels": r.permissions.manage_channels,
            "kick_members": r.permissions.kick_members,
            "ban_members": r.permissions.ban_members,
            "mention_everyone": r.permissions.mention_everyone,
            "manage_messages": r.permissions.manage_messages,
            "manage_webhooks": r.permissions.manage_webhooks,
        }.items() if v]
        roles.append(
            f"- {r.name} (pos {r.position}, {len(r.members)} members) perms: [{', '.join(flags) or 'none'}]"
        )

    channels = []
    for ch in guild.channels:
        overrides = []
        for target, perms in ch.overwrites.items():
            if isinstance(target, discord.Role):
                allow, deny = perms.pair()
                notable = []
                if allow.administrator: notable.append("+admin")
                if allow.manage_channels: notable.append("+manage_ch")
                if allow.manage_messages: notable.append("+manage_msg")
                if deny.send_messages: notable.append("-send")
                if deny.view_channel: notable.append("-view")
                if notable:
                    overrides.append(f"{target.name}=[{','.join(notable)}]")
        ov = f" overrides: {', '.join(overrides)}" if overrides else ""
        cat = f" (cat: {ch.category.name})" if ch.category else ""
        channels.append(f"- #{ch.name} [{ch.type}]{cat}{ov}")

    return (
        f"\n\n[Live server snapshot]\n"
        f"Name: {guild.name}\n"
        f"Members: {guild.member_count} "
        f"({sum(1 for m in guild.members if not m.bot)} humans, "
        f"{sum(1 for m in guild.members if m.bot)} bots)\n"
        f"Channels: {len(guild.channels)} | Roles: {len(guild.roles)}\n"
        f"Bot's top role: {guild.me.top_role.name} (position {guild.me.top_role.position})\n\n"
        f"ROLES (top to bottom):\n" + "\n".join(roles) +
        "\n\nCHANNELS:\n" + "\n".join(channels)
    )


# =============================================================================
# SYSTEM PROMPT
# =============================================================================
SYSTEM_PROMPT = """You are Claude Admin Bot, an expert Discord server administrator.
You have LIVE READ ACCESS to the user's Discord server (roles, channels, permissions, members).
You help the server owner analyze, fix, restructure, and improve their server.

You respond conversationally in plain English. Be direct, helpful, and concise.
Use the server snapshot provided to ground your answers in real data.

When you detect problems, point them out clearly. When asked to fix something,
describe what you would change in 1-3 sentences and ask for confirmation before
making destructive changes (deleting channels, kicking, banning).

You NEVER make up server data. If something isn't in the snapshot, say so.

If the user asks for a bot recommendation, suggest based on what you see:
- No welcome channel -> recommend a welcome bot (MEE6, Carl-bot)
- No mod log -> recommend a logging bot (Carl-bot, Dyno)
- Active community with raids -> recommend anti-raid (Wick, Beemo)
- Low engagement -> suggest engagement bots (Statbot, Leveling bots)
- Many channels in disarray -> suggest structure overhaul

Format with short paragraphs and bullet points. Keep responses under 1500 chars
unless the user asks for a full report."""


# =============================================================================
# EVENTS
# =============================================================================
@bot.event
async def on_ready():
    print(f"[READY] Logged in as {bot.user} (id={bot.user.id})", flush=True)
    guild = bot.get_guild(GUILD_ID)
    if guild:
        print(f"[READY] Connected to {guild.name} ({guild.id})", flush=True)
    else:
        print(f"[READY] WARNING: Not in target guild {GUILD_ID}", flush=True)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if not message.guild or message.guild.id != GUILD_ID:
        return
    if bot.user not in message.mentions:
        return
    if message.author.id != OWNER_ID:
        await message.channel.send(
            f"{message.author.mention} this bot only responds to its owner.",
            delete_after=5,
        )
        return

    content = message.content
    for mention in message.mentions:
        content = content.replace(f"<@{mention.id}>", "").replace(f"<@!{mention.id}>", "")
    content = content.strip()
    if not content:
        await message.channel.send("Yes? Ask me anything about your server.")
        return

    print(f"[MSG] {message.author}: {content[:200]}", flush=True)

    guild = message.guild
    snapshot = snapshot_server(guild)
    full_system = SYSTEM_PROMPT + snapshot

    history = conversation_memory.setdefault(message.channel.id, [])
    history.append({"role": "user", "content": content})
    history[:] = history[-20:]

    async with message.channel.typing():
        try:
            loop = asyncio.get_event_loop()
            reply = await loop.run_in_executor(
                None,
                lambda: call_openrouter(full_system, history[:-1], content, max_tokens=1500),
            )
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="ignore")[:500]
            print(f"[OR] HTTP {e.code}: {body}", flush=True)
            if e.code == 401:
                await message.channel.send("OpenRouter key invalid. Check OPENROUTER_API_KEY.")
            elif e.code == 402:
                await message.channel.send("OpenRouter: free tier limit or credits needed.")
            elif e.code == 429:
                await message.channel.send("OpenRouter rate limit. Wait a minute.")
            elif e.code == 404:
                await message.channel.send("OpenRouter model not found. Trying alt...")
            else:
                await message.channel.send(f"OpenRouter error {e.code}.")
            return
        except Exception as e:
            print(f"[OR] Error: {e}", flush=True)
            traceback.print_exc()
            await message.channel.send(f"Something went wrong: {e}")
            return

    print(f"[REPLY] {reply[:200]}", flush=True)

    history.append({"role": "assistant", "content": reply})

    if len(reply) <= 2000:
        await message.channel.send(reply)
    else:
        chunks = [reply[i:i+1900] for i in range(0, len(reply), 1900)]
        for chunk in chunks:
            await message.channel.send(chunk)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN, log_handler=None)