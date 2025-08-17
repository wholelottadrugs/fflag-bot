import os
import re
import json
import io
import discord
from discord.ext import commands

# ================= SETTINGS =================
BAN_CONTAINS = {"debounce", "decomp", "humanoid"}  # banned substrings
BAN_EXACT = set()   # e.g. {"DFIntS2PhysicsSenderRate"}
BAN_REGEX = []      # e.g. [r"^DFInt.*Bandwidth.*$"]

COMMAND_PREFIX = "!"
CLEAN_FILENAME = "cleared_list.json"
MAX_READ_BYTES = 1_000_000  # 1 MB safety
# ============================================

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)


def is_banned(name: str) -> bool:
    nlow = name.lower()
    if name in BAN_EXACT:
        return True
    if any(s in nlow for s in BAN_CONTAINS):
        return True
    if any(re.search(rx, name) for rx in BAN_REGEX):
        return True
    return False


def strip_code_fences(text: str) -> str:
    """Remove ```json ...``` fences if present."""
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        core = text[3:-3].strip()
        first_nl = core.find("\n")
        if first_nl != -1 and core[:first_nl].lower() in {"json", "txt"}:
            core = core[first_nl+1:]
        return core
    return text


def parse_fflags(raw: str) -> dict:
    raw = strip_code_fences(raw).lstrip("\ufeff")  # strip BOM if present

    # Try strict JSON first
    try:
        data = json.loads(raw)
        return {str(k): (v if isinstance(v, str) else str(v)) for k, v in data.items()}
    except Exception:
        pass

    # Fallback tolerant parser
    pairs = re.findall(r'"([^"]+)"\s*:\s*([^,\n}]+)', raw)
    out = {}
    for k, v in pairs:
        v = v.strip().rstrip(',')
        if len(v) >= 2 and ((v[0] == '"' and v[-1] == '"') or (v[0] == "'" and v[-1] == "'")):
            v = v[1:-1]
        out[k] = v
    return out


def filter_flags(ff: dict):
    kept, removed = {}, []
    for k, v in ff.items():
        if is_banned(k):
            removed.append((k, v))
        else:
            kept[k] = v
    return kept, removed


def to_json(d: dict) -> str:
    return json.dumps({k: str(v) for k, v in d.items()}, indent=4, ensure_ascii=False)


@bot.event
async def on_ready():
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="FFlags"))
    print(f"✅ Logged in as {bot.user} (id: {bot.user.id})")


@bot.command(name="scan", help="Attach a .txt/.json (or reply to one) and run !scan. You can also paste JSON.")
async def scan(ctx: commands.Context):
    att = None
    if ctx.message.attachments:
        att = ctx.message.attachments[0]
    elif ctx.message.reference:
        try:
            msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
            if msg.attachments:
                att = msg.attachments[0]
        except Exception:
            pass

    raw = None
    src_name = None

    if att:
        if att.size and att.size > MAX_READ_BYTES:
            return await ctx.reply("❌ File too large.", mention_author=False)
        data = await att.read()
        raw = data.decode("utf-8", errors="ignore")
        src_name = att.filename
    else:
        # Try inline JSON/code block text
        parts = ctx.message.content.split(" ", 1)
        if len(parts) > 1:
            raw = parts[1].strip()
            src_name = "message_content"

    if not raw:
        return await ctx.reply("Attach a file or paste JSON after `!scan`.", mention_author=False)

    fflags = parse_fflags(raw)
    if not fflags:
        return await ctx.reply("❌ Couldn’t parse any flags.", mention_author=False)

    kept, removed = filter_flags(fflags)
    cleaned_json = to_json(kept).encode("utf-8")
    files = [discord.File(io.BytesIO(cleaned_json), filename=CLEAN_FILENAME)]

    removed_lines = [f'"{k}": "{v}"' for k, v in removed]
    title = "Illegal Flags Found!" if removed else "No Illegal Flags Found"
    desc = f"Scan complete for **{src_name}**.\nRemoved **{len(removed)}** flag(s). Kept **{len(kept)}**."

    if removed_lines:
        preview = "\n".join(removed_lines)
        if len(preview) > 1500:
            preview = preview[:1500] + "\n… (truncated)"
        desc += "\n\n**Removed (preview):**\n```json\n" + preview + "\n```"

    embed = discord.Embed(
        title=title,
        description=desc,
        color=discord.Color.red() if removed else discord.Color.green()
    )
    await ctx.reply(embed=embed, files=files, mention_author=False)


@bot.command(name="status", help="Show bot health info")
async def status(ctx: commands.Context):
    await ctx.reply(
        f"✅ Online as **{bot.user}** | Prefix `{bot.command_prefix}`\n"
        f"• Banned substrings: {', '.join(sorted(BAN_CONTAINS)) or 'None'}",
        mention_author=False
    )


if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_TOKEN")
    if not TOKEN:
        raise SystemExit("❌ No DISCORD_TOKEN environment variable set.")
    bot.run(TOKEN)
