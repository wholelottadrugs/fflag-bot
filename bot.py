import os, re, json, io, csv, datetime, discord, aiosqlite
from discord.ext import commands

print("BOOT: starting")

BAN_CONTAINS = {"debounce", "decomp", "humanoid"}
COMMAND_PREFIX = "!"
CLEAN_FILENAME = "cleared_list.json"
MAX_READ_BYTES = 1_000_000
MAX_DB_TEXT = 500_000
DB_PATH = "bot.db"

intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)

db = None

async def init_db():
    global db
    print("DB: connecting…")
    db = await aiosqlite.connect(DB_PATH)
    await db.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS guilds(
        guild_id INTEGER PRIMARY KEY, name TEXT, joined_at TEXT, banned INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS scans(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER, user_id INTEGER, filename TEXT,
        removed_ct INTEGER, kept_ct INTEGER, created_at TEXT,
        kept_json TEXT, removed_json TEXT
    );
    """)
    await db.commit()
    print("DB: ready")

async def upsert_guild(g):
    await db.execute(
        "INSERT INTO guilds(guild_id,name,joined_at,banned) VALUES(?,?,?,0) "
        "ON CONFLICT(guild_id) DO UPDATE SET name=excluded.name",
        (g.id, g.name, datetime.datetime.utcnow().isoformat()))
    await db.commit()

async def is_guild_banned(gid:int)->bool:
    cur = await db.execute("SELECT banned FROM guilds WHERE guild_id=?", (gid,))
    row = await cur.fetchone()
    return bool(row[0]) if row else False

@bot.event
async def on_ready():
    print("READY: bot connected as", bot.user)
    await init_db()
    for g in bot.guilds:
        await upsert_guild(g)
    # leave banned if any
    for g in list(bot.guilds):
        if await is_guild_banned(g.id):
            try:
                if g.system_channel:
                    await g.system_channel.send("Leaving (banned).")
            except: pass
            await g.leave()
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="FFlags"))
    print("READY: presence set")

@bot.event
async def on_guild_join(guild):
    print("EVENT: joined", guild.id, guild.name)
    await upsert_guild(guild)
    if await is_guild_banned(guild.id):
        try:
            if guild.system_channel: await guild.system_channel.send("Leaving (banned).")
        except: pass
        await guild.leave()

def parse_fflags(raw:str)->dict:
    raw = raw.strip()
    if raw.startswith("```") and raw.endswith("```"):
        core = raw[3:-3].strip()
        nl = core.find("\n")
        if nl!=-1 and core[:nl].lower() in {"json","txt"}: core = core[nl+1:]
        raw = core
    raw = raw.lstrip("\ufeff")
    try:
        d = json.loads(raw); return {str(k): str(v) for k,v in d.items()}
    except: pass
    pairs = re.findall(r'"([^"]+)"\s*:\s*([^,\n}]+)', raw)
    out={}
    for k,v in pairs:
        v=v.strip().rstrip(',')
        if len(v)>=2 and ((v[0]==v[-1]=='"') or (v[0]==v[-1]=="'")): v=v[1:-1]
        out[k]=v
    return out

def filter_flags(ff:dict):
    kept, removed = {}, {}
    for k,v in ff.items():
        low = k.lower()
        if any(s in low for s in BAN_CONTAINS):
            removed[k]=v
        else:
            kept[k]=v
    return kept, removed

def to_json(d:dict)->str:
    return json.dumps({k:str(v) for k,v in d.items()}, indent=4, ensure_ascii=False)

@bot.command()
async def scan(ctx):
    print("CMD: scan called")
    att = ctx.message.attachments[0] if ctx.message.attachments else None
    raw = None; src = None
    if att:
        if att.size and att.size>MAX_READ_BYTES:
            return await ctx.reply("File too large.")
        raw = (await att.read()).decode("utf-8","ignore"); src=att.filename
    else:
        parts = ctx.message.content.split(" ",1)
        if len(parts)>1: raw=parts[1].strip(); src="message_content"
    if not raw: return await ctx.reply("Attach a file or paste JSON after `!scan`.")
    ff = parse_fflags(raw)
    if not ff: return await ctx.reply("Couldn't parse flags.")

    kept, removed = filter_flags(ff)
    kept_json = to_json(kept)
    removed_json = to_json(removed)

    # log
    await db.execute(
        "INSERT INTO scans(guild_id,user_id,filename,removed_ct,kept_ct,created_at,kept_json,removed_json)"
        "VALUES(?,?,?,?,?,?,?,?)",
        (ctx.guild.id if ctx.guild else 0, ctx.author.id, src or "",
         len(removed), len(kept), datetime.datetime.utcnow().isoformat(),
         kept_json[:MAX_DB_TEXT], removed_json[:MAX_DB_TEXT]))
    await db.commit()

    file = discord.File(io.BytesIO(kept_json.encode()), filename="cleared_list.json")
    title = "Illegal Flags Found!" if removed else "No Illegal Flags Found"
    desc = f"Removed **{len(removed)}** • Kept **{len(kept)}**."
    if removed:
        preview = "\n".join([f'"{k}": "{v}"' for k,v in removed.items()])
        if len(preview)>1500: preview = preview[:1500]+"\n… (truncated)"
        desc += "\n\n```json\n"+preview+"\n```"
    await ctx.reply(embed=discord.Embed(title=title, description=desc,
                                        color=discord.Color.red() if removed else discord.Color.green()),
                    file=file)

@bot.command()
@commands.is_owner()
async def banserver(ctx, guild_id:int):
    print("CMD: banserver", guild_id)
    await db.execute(
        "INSERT INTO guilds(guild_id,name,joined_at,banned) VALUES(?,?,?,1) "
        "ON CONFLICT(guild_id) DO UPDATE SET banned=1",
        (guild_id, "", datetime.datetime.utcnow().isoformat()))
    await db.commit()
    target = discord.utils.get(bot.guilds, id=guild_id)
    if target:
        try:
            if target.system_channel: await target.system_channel.send("Bot banned by owner. Leaving…")
        except: pass
        await target.leave()
        await ctx.reply(f"Banned and left `{guild_id}`.")
    else:
        await ctx.reply(f"Banned `{guild_id}`. If invited, bot will auto-leave.")

@bot.command()
@commands.is_owner()
async def banhere(ctx):
    if ctx.guild is None: return await ctx.reply("Run this in a server.")
    gid = ctx.guild.id
    print("CMD: banhere", gid)
    await db.execute(
        "INSERT INTO guilds(guild_id,name,joined_at,banned) VALUES(?,?,?,1) "
        "ON CONFLICT(guild_id) DO UPDATE SET banned=1",
        (gid, ctx.guild.name, datetime.datetime.utcnow().isoformat()))
    await db.commit()
    await ctx.reply("This server is now banned. Leaving…")
    await ctx.guild.leave()

@bot.event
async def on_guild_join(guild):
    print("EVENT: joined", guild.id, guild.name)
    await db.execute(
        "INSERT INTO guilds(guild_id,name,joined_at,banned) VALUES(?,?,?,0) "
        "ON CONFLICT(guild_id) DO UPDATE SET name=excluded.name",
        (guild.id, guild.name, datetime.datetime.utcnow().isoformat()))
    await db.commit()
    cur = await db.execute("SELECT banned FROM guilds WHERE guild_id=?", (guild.id,))
    row = await cur.fetchone()
    if row and row[0]:
        try:
            if guild.system_channel: await guild.system_channel.send("Leaving (banned).")
        except: pass
        await guild.leave()

@bot.check
async def block_banned(ctx):
    if ctx.guild is None: return True
    cur = await db.execute("SELECT banned FROM guilds WHERE guild_id=?", (ctx.guild.id,))
    row = await cur.fetchone()
    if row and row[0]:
        raise commands.CheckFailure("This server is banned.")
    return True

token = os.getenv("DISCORD_TOKEN")
if not token:
    raise SystemExit("No DISCORD_TOKEN set")
bot.run(token)
