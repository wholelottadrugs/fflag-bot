import re, json, io, discord
from discord.ext import commands

# =========== SETTINGS ===========
# Remove any flag whose NAME contains any of these substrings (case-insensitive)
BAN_CONTAINS = {"debounce", "decomp", "humanoid"}

# Optional extras if you ever need them:
BAN_EXACT = set()   # e.g., {"DFIntS2PhysicsSenderRate"}
BAN_REGEX = []      # e.g., [r"^DFInt.*Bandwidth.*$"]

COMMAND_PREFIX = "!"
CLEAN_FILENAME = "cleared_list.json"
REMOVED_FILENAME = "removed_flags.txt"
# ================================

TOKEN = "MTQwNjcwOTM3NzE5MjQzMTYzOA.Ght7kQ.SYpSwVcba8PuVEyuCB-Q8_uNvlujNDFqZEE7dQ"  # <- put your token between quotes

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)

def is_banned(name: str) -> bool:
    nlow = name.lower()
    if name in BAN_EXACT:
        return True
    if any(s in nlow for s in (s.lower() for s in BAN_CONTAINS)):
        return True
    if any(re.search(rx, name) for rx in BAN_REGEX):
        return True
    return False

def parse_fflags(raw: str) -> dict:
    """
    Accepts valid JSON or loose JSON-ish lists of "Key": value lines.
    Returns {key: value_as_string}.
    """
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
    # Keep values as strings for consistent output
    return json.dumps({k: str(v) for k, v in d.items()}, indent=4, ensure_ascii=False)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")

@bot.command(name="scan", help="Attach an FFlag file and run !scan (or reply to a file with !scan)")
async def scan(ctx: commands.Context):
    # Find an attachment either on this message or the replied-to message
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

    if not att:
        return await ctx.reply("Attach a `.txt` or `.json` with your FFlags (or reply to one) and run `!scan`.")

    raw = (await att.read()).decode("utf-8", errors="ignore")
    fflags = parse_fflags(raw)
    kept, removed = filter_flags(fflags)

    cleaned_json = to_json(kept).encode("utf-8")
    removed_lines = [f'"{k}": "{v}"' for k, v in removed]
    removed_txt = ("\n".join(removed_lines) if removed_lines else "—").encode("utf-8")

    files = [
        discord.File(io.BytesIO(cleaned_json), filename=CLEAN_FILENAME),
        discord.File(io.BytesIO(removed_txt), filename=REMOVED_FILENAME),
    ]

    title = "Illegal Flags Found!" if removed else "No Illegal Flags Found"
    desc = f"Scan complete for **{att.filename}**.\nRemoved **{len(removed)}** flag(s). Cleaned list attached as `{CLEAN_FILENAME}`."

    if removed_lines:
        preview = "\n".join(removed_lines)[:1500]
        desc += "\n\n**Removed Lines (preview):**\n```json\n" + preview + "\n```"

    embed = discord.Embed(
        title=title,
        description=desc,
        color=discord.Color.red() if removed else discord.Color.green()
    )

    await ctx.reply(embed=embed, files=files, mention_author=False)

if __name__ == "__main__":
    bot.run(TOKEN)
