"""
Claude Admin Bot - conversational, mention-driven, server-aware.
Uses Groq (free Llama 3.3 70B) as the brain.
"""

import os
import sys
import json
import asyncio
import datetime
import traceback
import urllib.request
import urllib.error
from typing import Optional

import discord
from discord.ext import commands

# Force unbuffered prints so Railway logs are real-time
print("[BOOT] Starting Claude Admin Bot", flush=True)
print(f"[BOOT] Python {sys.version.split()[0]}", flush=True)
print(f"[BOOT] discord.py {discord.__version__}", flush=True)

# --- Secrets ---
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID_STR = os.environ.get("GUILD_ID")
OWNER_ID_STR = os.environ.get("OWNER_ID")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

for name, val in [
    ("DISCORD_TOKEN", DISCORD_TOKEN),
    ("GUILD_ID", GUILD_ID_STR),
    ("OWNER_ID", OWNER_ID_STR),
    ("GROQ_API_KEY", GROQ_API_KEY),
]:
    if not val:
        print(f"[FATAL] {name} is missing!", flush=True)
        sys.exit(1)

GUILD_ID = int(GUILD_ID_STR)
OWNER_ID = int(OWNER_ID_STR)

print(f"[BOOT] Guild={GUILD_ID} Owner={OWNER_ID} GroqKey={'set' if GROQ_API_KEY else 'MISSING'}", flush=True)

# --- Discord bot setup ---
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

# Conversation memory per channel (last 20 messages). Resets on restart.
conversation_memory: dict[int, list[dict]] = {}
PENDING_ACTIONS: dict[int, dict] = {}  # channel_id -> {action_id, description, payload}


# =============================================================================
# GROQ API CALL
# =============================================================================
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"


def call_groq(messages: list[dict], max_tokens: int = 1024) -> str:
    """Synchronous Groq call. Returns assistant text."""
    payload = {
        "model": GROQ_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.4,
    }
    req = urllib.request.Request(
        GROQ_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"].strip()


# =============================================================================
# SERVER STATE SNAPSHOT
# =============================================================================
def snapshot_server(guild: discord.Guild) -> dict:
    """Compact JSON view of the server for the LLM."""
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
        roles.append({
            "name": r.name,
            "id": str(r.id),
            "position": r.position,
            "members": len(r.members),
            "permissions": flags or ["none"],
        })

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
                    overrides.append({"role": target.name, "flags": notable})
        channels.append({
            "name": ch.name,
            "id": str(ch.id),
            "type": str(ch.type),
            "category": ch.category.name if ch.category else None,
            "overrides": overrides,
        })

    return {
        "server_name": guild.name,
        "server_id": str(guild.id),
        "member_count": guild.member_count,
        "human_count": sum(1 for m in guild.members if not m.bot),
        "bot_count": sum(1 for m in guild.members if m.bot),
        "owner_id": str(guild.owner_id) if guild.owner_id else None,
        "created_at": guild.created_at.isoformat(),
        "bot_top_role": guild.me.top_role.name,
        "bot_top_role_position": guild.me.top_role.position,
        "roles_count": len(roles),
        "channels_count": len(channels),
        "roles": roles,
        "channels": channels,
    }


# =============================================================================
# SYSTEM PROMPT
# =============================================================================
SYSTEM_PROMPT = """You are Claude Admin Bot, an expert Discord server administrator.
You have LIVE READ ACCESS to the user's Discord server (roles, channels, permissions, members).
You help the server owner analyze, fix, restructure, and improve their server.

You respond conversationally in plain English. Be direct, helpful, and concise.
Use the server snapshot provided to ground your answers in real data.

When you detect problems, point them out clearly. When asked to fix something,
describe what you'd change in 1-3 sentences and ask for confirmation before
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
    # Ignore ourselves and other bots
    if message.author.bot:
        return
    if not message.guild or message.guild.id != GUILD_ID:
        return

    # Only respond when the bot is mentioned
    if bot.user not in message.mentions:
        return

    # Only owner can talk to the bot for now
    if message.author.id != OWNER_ID:
        await message.channel.send(
            f"{message.author.mention} this bot only responds to its owner.",
            delete_after=5,
        )
        return

    # Strip the mention to get the actual question
    content = message.content
    for mention in message.mentions:
        content = content.replace(f"@{mention.display_name}", "").replace(f"<@{mention.id}>", "").replace(f"<@!{mention.id}>", "")
    content = content.strip()
    if not content:
        await message.channel.send("Yes? Ask me anything about your server.")
        return

    print(f"[MSG] {message.author}: {content[:200]}", flush=True)

    # Build context: system + server snapshot + conversation history + user msg
    guild = message.guild
    snapshot = snapshot_server(guild)
    snapshot_text = (
        f"\n\n[Live server snapshot]\n"
        f"Name: {snapshot['server_name']}\n"
        f"Members: {snapshot['member_count']} ({snapshot['human_count']} humans, {snapshot['bot_count']} bots)\n"
        f"Channels: {snapshot['channels_count']} | Roles: {snapshot['roles_count']}\n"
        f"Bot's top role: {snapshot['bot_top_role']} (position {snapshot['bot_top_role_position']})\n\n"
        f"ROLES (top to bottom):\n"
        + "\n".join(
            f"- {r['name']} (pos {r['position']}, {r['members']} members) perms: {', '.join(r['permissions'])}"
            for r in snapshot["roles"]
        )
        + "\n\nCHANNELS:\n"
        + "\n".join(
            f"- #{c['name']} ({c['type']}, cat={c['category']})"
            + (f" overrides: {json.dumps(c['overrides'])}" if c["overrides"] else "")
            for c in snapshot["channels"]
        )
    )

    # Conversation memory (last 20)
    history = conversation_memory.setdefault(message.channel.id, [])
    history.append({"role": "user", "content": content})
    history[:] = history[-20:]

    messages_for_llm = [
        {"role": "system", "content": SYSTEM_PROMPT + snapshot_text},
        *[{"role": m["role"], "content": m["content"]} for m in history],
    ]

    # Show "typing..." while we wait
    async with message.channel.typing():
        try:
            loop = asyncio.get_event_loop()
            reply = await loop.run_in_executor(
                None,
                lambda: call_groq(messages_for_llm, max_tokens=1500),
            )
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="ignore")[:500]
            print(f"[GROQ] HTTP {e.code}: {body}", flush=True)
            await message.channel.send(f"Groq API error {e.code}. Check your GROQ_API_KEY.")
            return
        except Exception as e:
            print(f"[GROQ] Error: {e}", flush=True)
            traceback.print_exc()
            await message.channel.send(f"Something went wrong talking to Groq: {e}")
            return

    print(f"[REPLY] {reply[:200]}", flush=True)

    # Save assistant reply to memory
    history.append({"role": "assistant", "content": reply})

    # Send reply (Discord limit 2000 chars per message)
    if len(reply) <= 2000:
        await message.channel.send(reply)
    else:
        # Chunk it
        chunks = [reply[i:i+1900] for i in range(0, len(reply), 1900)]
        for chunk in chunks:
            await message.channel.send(chunk)


# =============================================================================
# Run
# =============================================================================
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN, log_handler=None)