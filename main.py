"""
Claude Admin Bot - conversational, mention-driven, server-aware.
Uses Google Gemini (free) as the brain.
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
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

for name, val in [
    ("DISCORD_TOKEN", DISCORD_TOKEN),
    ("GUILD_ID", GUILD_ID_STR),
    ("OWNER_ID", OWNER_ID_STR),
    ("GEMINI_API_KEY", GEMINI_API_KEY),
]:
    if not val:
        print(f"[FATAL] {name} is missing!", flush=True)
        sys.exit(1)

GUILD_ID = int(GUILD_ID_STR)
OWNER_ID = int(OWNER_ID_STR)

print(f"[BOOT] Guild={GUILD_ID} Owner={OWNER_ID} GeminiKey={'set' if GEMINI_API_KEY else 'MISSING'}", flush=True)

# --- Bot ---
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

conversation_memory: dict[int, list[dict]] = {}

# =============================================================================
# GEMINI API CALL
# =============================================================================
# Try multiple model names in order until one works (handles regional/account variance)
GEMINI_MODELS = [
    "gemini-1.5-flash",
    "gemini-1.5-flash-latest",
    "gemini-1.5-flash-8b",
    "gemini-1.5-pro",
    "gemini-pro",
]


def call_gemini(system_prompt: str, history: list[dict], user_msg: str, max_tokens: int = 1500) -> str:
    """Synchronous Gemini call. Tries multiple models until one works."""
    contents = []
    for msg in history:
        role = "model" if msg["role"] == "assistant" else "user"
        contents.append({
            "role": role,
            "parts": [{"text": msg["content"]}],
        })
    contents.append({
        "role": "user",
        "parts": [{"text": user_msg}],
    })

    payload = {
        "systemInstruction": {
            "parts": [{"text": system_prompt}]
        },
        "contents": contents,
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": 0.4,
        },
    }

    last_error = None
    for model in GEMINI_MODELS:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={GEMINI_API_KEY}"
        )
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            candidates = data.get("candidates", [])
            if not candidates:
                print(f"[GEMINI] {model}: no candidates in response", flush=True)
                last_error = "no candidates"
                continue
            parts = candidates[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts).strip()
            if text:
                print(f"[GEMINI] Using model: {model}", flush=True)
                return text
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="ignore")[:300]
            print(f"[GEMINI] {model} -> HTTP {e.code}: {body}", flush=True)
            last_error = f"HTTP {e.code}"
            if e.code in (400, 403):  # bad request or auth - won't help to retry
                continue
            continue
        except Exception as e:
            print(f"[GEMINI] {model} -> {e}", flush=True)
            last_error = str(e)
            continue

    raise RuntimeError(f"All Gemini models failed. Last error: {last_error}")


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

    # Strip the mention
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
                lambda: call_gemini(full_system, history[:-1], content, max_tokens=1500),
            )
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="ignore")[:500]
            print(f"[GEMINI] HTTP {e.code}: {body}", flush=True)
            if e.code == 429:
                await message.channel.send("Gemini rate limit hit. Wait a minute and try again.")
            elif e.code == 403:
                await message.channel.send("Gemini API key invalid or region blocked. Check your GEMINI_API_KEY.")
            else:
                await message.channel.send(f"Gemini API error {e.code}. Check logs.")
            return
        except Exception as e:
            print(f"[GEMINI] Error: {e}", flush=True)
            traceback.print_exc()
            await message.channel.send(f"Something went wrong talking to Gemini: {e}")
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