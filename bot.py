import os, re, json, io, csv, datetime
import discord
from discord.ext import commands
import aiosqlite

# ================= SETTINGS =================
BAN_CONTAINS = {"debounce", "decomp", "humanoid"}  # substrings in FLAG NAMES (case-insensitive)
BAN_EXACT = set()                                  # e.g. {"DFIntS2PhysicsSenderRate"}
BAN_REGEX = []                                     # e.g. [r"^DFInt.*Bandwidth.*$"]

COMMAND_PREFIX = "!"
CLEAN_FILENAME = "cleared_list.json"
MAX_READ_BYTES = 1_000_000       # Ignore attachments larger than this
MAX_DB_TEXT = 500_000            # Trim very large JSON blobs before storing (safety)

DB_PATH = "bot.db"               # On Railway this is ephemeral; use dump/export to save data
# ============================================

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)

# ---------- DB ----------
db: aiosqlite.Connection | None = None

async def init_db():
    """Create/open SQLite and ensure tables exist."""
    global db
    db = await aiosqlite.connect(DB_PATH)
    await db.executescript("""
    PRAGMA journal_mode=WAL;

    CREATE TABLE IF NOT EXISTS guilds (
        guild_id    INTEGER PRIMARY KEY,
        name        TEXT,
        joined_at   TEXT,
        banned      INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS scans (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id      INTEGER,
        user_id       INTEGER,
        filename      TEXT,
        removed_ct    INTEGER,
        kept_ct       INTEGER,
        created_at    TEXT,
        raw_flags     TEXT,   -- original text provided
        kept_json     TEXT,   -- cleaned JSON we sent back
        removed_json  TEXT    -- JSON object of removed flags
    );
    """)
    await db.commit()

async def upsert_guild(g: discord.Guild):
    if db is None: return
    await db.execute(
        "INSERT INTO guilds(guild_id,name,joined_at,banned) VALUES(?,?,?,0) "
        "ON CONFLICT(guild_id) DO UPDATE SET name=excluded.name",
        (g.id, g.name, datetime.datetime.utcnow().isoformat())
    )
    await db.commit()

async def set_guild_ban(guild_id: int, banned: int):
    if db is None: return
    await db.execute(
        "INSERT INTO guilds(guild_id,name,joined_at,banned) VALUES(?,?,?,?) "
        "ON CONFLICT(guild_id) DO UPDATE SET banned=excluded.banned",
        (guild_id, "", datetime.datetime.utcnow().isoformat(), banned)
    )
    await db.commit()

async def is_guild_banned(guild_id: int) -> bool:
    if db is None: return False
    cur = await db.execute("SELECT banned FROM guilds WHERE guild_id=?", (guild_id,))
    row = await cur.fetchone()
    return bool(row[0]) if row else False

async def log_scan(guild_id: int, user_id: int, filename: str,
                   removed_ct: int, kept_ct: int,
                   raw_flags: str, kept_json: str, removed_json: str) -> int:
    """Insert a scan row and return its id."""
    if db is None: return 0
    # Trim huge blobs defensively
    raw_flags   = raw_flags[:MAX_DB_TEXT]
    kept_json   = kept_json[:MAX_DB_TEXT]
    removed_json= removed_json[:MAX_DB_TEXT]

    await db.execute(
        "INSERT INTO scans(guild_id,user_id,filename,removed_ct,kept_ct,created_at,raw_flags,kept_json,removed_json) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (guild_id, user_id, filename, removed_ct, kept_ct,
         datetime.datetime.utcnow().isoformat(), raw_flags, kept_json, removed_json)
    )
    await db.commit()
    cur = await db.execute("SELECT last_insert_rowid()")
    return (await cur.fetchone())[0]

# ---------- FFlag helpers ----------
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

    # Fallback tolerant parser for lines like  "Key": value
    pairs = re.findall(r'"([^"]+)"\s*:\s*([^,\n}]+)', raw)
    out = {}
    for k, v in pairs:
        v = v.strip().rstrip(',')
        if len(v) >= 2 and ((v[0] == '"' and v[-1] == '"') or (v[0] == "'" and v[-1] == "'")):
            v = v[1:-1]
        out[k] = v
    return out

def filter_flags(ff: dict):
    kept, removed = {}, {}
    for k, v in ff.items():
        if is_banned(k):
            removed[k] = v
        else:
            kept[k] = v
    return kept, removed

def to_json(d: dict) -> str:
    return json.dumps({k: str(v) for k, v in d.items()}, indent=4, ensure_ascii=False)

# ---------- Bot lifecycle ----------
@bot.event
async def on_ready():
    await init_db()
    for g in bot.guilds:
        await upsert_guild(g)
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="FFlags"))
    print(f"✅ Logged in as {bot.user} | Guilds: {len(bot.guilds)}")

@bot.event
async def on_guild_join(guild: discord.Guild):
    await upsert_guild(guild)
    if await is_guild_banned(guild.id):
        try:
            if guild.system_channel:
                await guild.system_channel.send("🚫 This bot is banned on this server. Leaving…")
        except Exception:
            pass
        await guild.leave()

# Gate banned guilds from running commands
@bot.check
async def guild_gate(ctx: commands.Context):
    if ctx.guild is None:
        return True
    if await is_guild_banned(ctx.guild.id):
        raise commands.CheckFailure("This server is banned from using this bot.")
    return True

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        await ctx.reply("❌ Unknown command. Try `!scan` or `!status`.", mention_author=False)
    elif isinstance(error, commands.CheckFailure):
        await ctx.reply("🚫 This server is banned from using this bot.", mention_author=False)
    else:
        await ctx.reply(f"⚠️ Error: {error}", mention_author=False)

# ---------- Commands ----------
@bot.command(name="scan", help="Attach a .txt/.json (or reply/paste JSON) then run !scan.")
async def scan(ctx: commands.Context):
    # 1) Get data: attachment > replied message attachment > inline JSON
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

    raw, src_name = None, None
    if att:
        if att.size and att.size > MAX_READ_BYTES:
            return await ctx.reply("❌ File too large.", mention_author=False)
        raw = (await att.read()).decode("utf-8", errors="ignore")
        src_name = att.filename
    else:
        parts = ctx.message.content.split(" ", 1)
        if len(parts) > 1:
            raw = parts[1].strip()
            src_name = "message_content"

    if not raw:
        return await ctx.reply("Attach a file or paste JSON after `!scan`.", mention_author=False)

    # 2) Parse & filter
    fflags = parse_fflags(raw)
    if not fflags:
        return await ctx.reply("❌ Couldn’t parse any flags.", mention_author=False)

    kept, removed = filter_flags(fflags)
    kept_json_str = to_json(kept)
    removed_json_str = to_json(removed)

    cleaned_json_bytes = kept_json_str.encode("utf-8")
    files = [discord.File(io.BytesIO(cleaned_json_bytes), filename=CLEAN_FILENAME)]

    # 3) Log to DB (store raw flags + kept + removed)
    scan_id = await log_scan(
        ctx.guild.id if ctx.guild else 0,
        ctx.author.id,
        src_name or "",
        removed_ct=len(removed),
        kept_ct=len(kept),
        raw_flags=raw,
        kept_json=kept_json_str,
        removed_json=removed_json_str
    )

    # 4) Respond
    title = "Illegal Flags Found!" if removed else "No Illegal Flags Found"
    desc = (
        f"Scan **#{scan_id}** for **{src_name}**\n"
        f"Removed **{len(removed)}** • Kept **{len(kept)}**."
    )

    if removed:
        preview_lines = [f'"{k}": "{v}"' for k, v in removed.items()]
        preview = "\n".join(preview_lines)
        if len(preview) > 1500:
            preview = preview[:1500] + "\n… (truncated)"
        desc += "\n\n**Removed (preview):**\n```json\n" + preview + "\n```"

    embed = discord.Embed(
        title=title,
        description=desc,
        color=discord.Color.red() if removed else discord.Color.green()
    )
    await ctx.reply(embed=embed, files=files, mention_author=False)

@bot.command(name="status", help="Bot health and counts.")
async def status(ctx: commands.Context):
    total_guilds = len(bot.guilds)
    banned_guilds = total_scans = 0
    if db:
        cur = await db.execute("SELECT COUNT(*) FROM guilds WHERE banned=1")
        banned_guilds = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM scans")
        total_scans = (await cur.fetchone())[0]
    await ctx.reply(
        f"✅ Online as **{bot.user}** | Prefix `{bot.command_prefix}`\n"
        f"• Guilds: {total_guilds} (banned: {banned_guilds})\n"
        f"• Total scans: {total_scans}\n"
        f"• Banned substrings: {', '.join(sorted(BAN_CONTAINS))}",
        mention_author=False
    )

# ----- Owner-only admin -----
@bot.command(name="servers", help="List first 20 servers (owner-only).")
@commands.is_owner()
async def servers(ctx: commands.Context):
    if not db:
        return await ctx.reply("DB not ready.", mention_author=False)
    cur = await db.execute("SELECT guild_id,name,banned FROM guilds ORDER BY banned DESC, name LIMIT 20")
    rows = await cur.fetchall()
    if not rows:
        return await ctx.reply("No guilds recorded yet.", mention_author=False)
    lines = [f"{'🚫' if r[2] else '✅'} {r[1] or '(unknown)'} — `{r[0]}`" for r in rows]
    await ctx.reply("**Servers (first 20):**\n" + "\n".join(lines), mention_author=False)

@bot.command(name="banserver", help="Ban a server by ID (owner-only).")
@commands.is_owner()
async def banserver(ctx: commands.Context, guild_id: int):
    await set_guild_ban(guild_id, 1)
    if ctx.guild and ctx.guild.id == guild_id:
        await ctx.reply("🚫 Banned this server. Leaving…", mention_author=False)
        await ctx.guild.leave()
    else:
        await ctx.reply(f"🚫 Banned server `{guild_id}`.", mention_author=False)

@bot.command(name="unbanserver", help="Unban a server by ID (owner-only).")
@commands.is_owner()
async def unbanserver(ctx: commands.Context, guild_id: int):
    await set_guild_ban(guild_id, 0)
    await ctx.reply(f"✅ Unbanned server `{guild_id}`.", mention_author=False)

@bot.command(name="exportscans", help="Export scan metadata as CSV (owner-only).")
@commands.is_owner()
async def exportscans(ctx: commands.Context):
    if not db:
        return await ctx.reply("DB not ready.", mention_author=False)
    cur = await db.execute(
        "SELECT id,guild_id,user_id,filename,removed_ct,kept_ct,created_at "
        "FROM scans ORDER BY id DESC LIMIT 5000"
    )
    rows = await cur.fetchall()
    if not rows:
        return await ctx.reply("No scans logged yet.", mention_author=False)

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id","guild_id","user_id","filename","removed_ct","kept_ct","created_at"])
    w.writerows(rows)
    data = io.BytesIO(buf.getvalue().encode("utf-8"))
    await ctx.reply(file=discord.File(data, filename="scans_export.csv"), mention_author=False)

@bot.command(name="scanjson", help="Download full JSON for a scan id (owner-only). Usage: !scanjson 12")
@commands.is_owner()
async def scanjson(ctx: commands.Context, scan_id: int):
    if not db:
        return await ctx.reply("DB not ready.", mention_author=False)
    cur = await db.execute(
        "SELECT kept_json, removed_json, raw_flags FROM scans WHERE id=?", (scan_id,)
    )
    row = await cur.fetchone()
    if not row:
        return await ctx.reply("Scan id not found.", mention_author=False)

    payload = {
        "id": scan_id,
        "kept": json.loads(row[0]) if row[0] else {},
        "removed": json.loads(row[1]) if row[1] else {},
        "raw_flags": row[2] or ""
    }
    data = io.BytesIO(json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"))
    await ctx.reply(file=discord.File(data, filename=f"scan_{scan_id}.json"), mention_author=False)

@bot.command(name="dumpdb", help="Send the raw SQLite DB file (owner-only).")
@commands.is_owner()
async def dumpdb(ctx: commands.Context):
    if not os.path.exists(DB_PATH):
        return await ctx.reply("No DB file yet.", mention_author=False)
    await ctx.reply(file=discord.File(DB_PATH), mention_author=False)

# ---------- Run ----------
if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("❌ No DISCORD_TOKEN environment variable set.")
    bot.run(token)
