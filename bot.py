# bot.py
import os, re, io, json, hashlib
import discord
from discord.ext import commands

# --- Config ---
TOKEN = os.getenv("DISCORD_TOKEN")  # set your bot token in env
PREFIX = "!"
INT_MIN, INT_MAX = -2147483648, 2147483647

# --- Rules ---
PREFIX_RULES = {
    "DFFlag": "bool", "FFlag": "bool",
    "DFInt": "int",   "FInt": "int",
    "DFString":"str", "FString":"str",
    "DFLog":"int",    "FLog":"int",
    "DFBool":"bool",  "FBool":"bool",
}
KEY_VALID_RE = re.compile(r"^(?:DFFlag|FFlag|DFInt|FInt|DFString|FString|DFLog|FLog|DFBool|FBool)[A-Za-z0-9_]*$")
ILLEGAL_NAME_PATTERNS = [
    re.compile(r"debounce", re.I),
    re.compile(r"decomp",   re.I),
    re.compile(r"humanoid", re.I),
]

# --- Discord setup ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

# --- Helpers ---
def extract_json_from_message(content: str) -> str | None:
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.S | re.I)
    if m: return m.group(1)
    m = re.search(r"(\{.*\})", content, re.S)
    if m: return m.group(1)
    return None

def coerce_value(expected_type: str, v):
    if expected_type == "bool":
        if isinstance(v, bool): return v, None
        if isinstance(v, str) and v.strip().lower() in ("true","false"):
            return v.strip().lower() == "true", "string_bool_fixed"
        return None, "bad_type_bool"

    if expected_type == "int":
        if isinstance(v, int):
            if INT_MIN <= v <= INT_MAX: return v, None
            return None, "int_out_of_range"
        if isinstance(v, str) and re.fullmatch(r"-?\d+", v.strip()):
            iv = int(v.strip())
            if INT_MIN <= iv <= INT_MAX: return iv, "string_int_fixed"
            return None, "int_out_of_range"
        return None, "bad_type_int"

    if expected_type == "str":
        if isinstance(v, str): return v, None
        if isinstance(v, (int, bool, float)): return str(v), "primitive_to_string"
        return None, "bad_type_str"

    return None, "unknown_type"

def remove_illegal(data: dict):
    illegal = []
    kept = {}
    for k, v in data.items():
        if any(p.search(k) for p in ILLEGAL_NAME_PATTERNS):
            illegal.append(k)
        else:
            kept[k] = v
    return kept, illegal

def clean_flags(data: dict):
    """
    Drops invalid keys (bad prefix) and unfixable type errors.
    Coerces simple mistakes (string->int/bool, primitives->string).
    """
    cleaned = {}
    dropped_invalid = []
    fixed_notes = []

    for k, v in data.items():
        if not KEY_VALID_RE.match(k):
            dropped_invalid.append(k)
            continue
        # infer expected type by prefix
        expected = None
        for pref, t in PREFIX_RULES.items():
            if k.startswith(pref):
                expected = t
                break
        if not expected:
            dropped_invalid.append(k)
            continue

        new_v, note = coerce_value(expected, v)
        if new_v is None:
            dropped_invalid.append(k)
            continue
        if note: fixed_notes.append(f"{k}: {note}")
        cleaned[k] = new_v

    return cleaned, dropped_invalid, fixed_notes

def to_pretty_json(d: dict) -> str:
    return json.dumps(d, ensure_ascii=False, indent=2, sort_keys=True)

# --- Commands ---
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")

@bot.command(name="scan")
async def scan(ctx: commands.Context, *, maybe_json: str = None):
    """
    !scan  →  (1) remove illegal flags  →  (2) clean flags (remove invalid / fix simple types).
    Provide JSON by attaching .json/.txt OR paste inline (raw or ```json fenced```).
    """
    # read input
    raw_text = None
    if ctx.message.attachments:
        for att in ctx.message.attachments:
            if att.filename.lower().endswith((".json",".txt")):
                raw_text = (await att.read()).decode("utf-8", errors="replace")
                break
    if raw_text is None:
        if maybe_json:
            raw_text = extract_json_from_message(maybe_json) or maybe_json
        else:
            raw_text = extract_json_from_message(ctx.message.content)

    if not raw_text:
        return await ctx.reply("Attach/paste your flags JSON. Example: `!scan { \"DFFlagFoo\": true }`")

    # parse
    try:
        data = json.loads(raw_text)
        if not isinstance(data, dict):
            raise ValueError("Top-level must be an object.")
    except Exception as e:
        return await ctx.reply(f"❌ JSON parse error: {e}")

    # (1) remove illegal
    step1, illegal_keys = remove_illegal(data)

    # (2) clean flags
    cleaned, dropped_invalid, fixed_notes = clean_flags(step1)

    # outputs
    cleaned_json = to_pretty_json(cleaned)
    digest = hashlib.sha256(cleaned_json.encode("utf-8")).hexdigest()[:8]
    cleaned_fname = f"fflags_cleaned_{digest}.json"

    # brief report
    lines = []
    lines.append("**Scan result**")
    lines.append(f"- Input keys: `{len(data)}`")
    lines.append(f"- Removed (illegal): `{len(illegal_keys)}`")
    lines.append(f"- Dropped (invalid/unfixable): `{len(dropped_invalid)}`")
    lines.append(f"- Kept: `{len(cleaned)}`")
    if illegal_keys:
        lines.append(f"- Illegal sample: {', '.join(illegal_keys[:10])}" + (" ..." if len(illegal_keys)>10 else ""))
    if fixed_notes:
        lines.append(f"- Coercions: `{len(fixed_notes)}`")

    # attach cleaned JSON + verbose (optional)
    files = []
    files.append(discord.File(io.BytesIO(cleaned_json.encode("utf-8")), filename=cleaned_fname))

    verbose = io.StringIO()
    if illegal_keys:
        verbose.write("=== Illegal removed ===\n")
        for k in illegal_keys: verbose.write(k + "\n")
        verbose.write("\n")
    if dropped_invalid:
        verbose.write("=== Invalid/unfixable dropped ===\n")
        for k in dropped_invalid: verbose.write(k + "\n")
        verbose.write("\n")
    if fixed_notes:
        verbose.write("=== Coercions applied ===\n")
        for n in fixed_notes: verbose.write(n + "\n")
    files.append(discord.File(io.BytesIO(verbose.getvalue().encode("utf-8")), filename="scan_report.txt"))

    await ctx.reply("\n".join(lines), files=files, mention_author=False)

@bot.command(name="help")
async def help_cmd(ctx: commands.Context):
    await ctx.reply(
        f"**Commands**\n- `{PREFIX}scan` → remove illegal flags, then clean flags. Attach or paste JSON.",
        mention_author=False
    )

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN env var.")
    bot.run(TOKEN)
