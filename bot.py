import os
import re
import json
import base64
import sqlite3
import asyncio
from dataclasses import dataclass
from typing import Optional, List, Dict, Any
from urllib.parse import urlencode

import discord
from discord import app_commands
from discord.ext import commands, tasks

import aiohttp
import feedparser

# Gmail API (official)
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

# ==========================================================
# CONFIG (set via Environment Variables on Railway)
# ==========================================================
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "").strip()

DEFAULT_ZIP = os.environ.get("ZIP", "95673").strip()
DEFAULT_RADIUS_MILES = int(os.environ.get("RADIUS_MILES", "20"))

POLL_SECONDS_LOCAL = int(os.environ.get("POLL_SECONDS_LOCAL", "180"))     # 3 min
POLL_SECONDS_ONLINE = int(os.environ.get("POLL_SECONDS_ONLINE", "300"))   # 5 min
POLL_SECONDS_GMAIL = int(os.environ.get("POLL_SECONDS_GMAIL", "180"))     # 3 min

INCLUDE_KEYWORDS = [k.strip().lower() for k in os.environ.get("INCLUDE_KEYWORDS", "").split(",") if k.strip()]
EXCLUDE_KEYWORDS = [k.strip().lower() for k in os.environ.get("EXCLUDE_KEYWORDS", "").split(",") if k.strip()]

# On hosts with ephemeral disks, this DB may reset between deploys.
# That only affects "seen" dedupe history.
DB_PATH = os.environ.get("DB_PATH", "freebie_radar.db")

SETUP_CATEGORY_NAME = "Freebie Radar"
ROLE_LOCAL = "Freebie Radar Local"
ROLE_ONLINE = "Freebie Radar Online"

CHANNELS_TO_CREATE = [
    ("free-local", "Local pickup freebies (Craigslist + FB Marketplace email alerts)."),
    ("free-online", "Online freebies (Steam/Epic/GOG/Humble + Reddit + tech feeds)."),
    ("requests", "Family requests: looking for ____."),
    ("claimed-and-dead", "Claimed items + expired links."),
]

# Craigslist Sacramento free stuff
CRAIGSLIST_BASE = "https://sacramento.craigslist.org/search/zip"

# Steam sources
STEAMDB_FREE_PROMOS_URL = "https://steamdb.info/upcoming/free/"

# GamerPower API aggregator (Steam/Epic/GOG)
GAMERPOWER_API = "https://www.gamerpower.com/api"

# Reddit RSS subs (edit as you like)
DEFAULT_REDDIT_SUBS = [
    "GameDeals",
    "FreeGameFindings",
    "FreeGamesOnSteam",
    "epicgamespc",
    "gog",
    "humblebundles",
    "Gamebundles",
    "AppHookup",
    "freebies",
    "software",
]

# Tech/software giveaway RSS feeds (best-effort; add/remove later)
DEFAULT_TECH_RSS = [
    "https://blog.humblebundle.com/rss",
    "https://www.epicbundle.com/feed/",
    "https://www.makeuseof.com/tag/freebies/feed/",
]

# Gmail integration (Facebook Marketplace notifications land in Gmail)
# Uses Gmail API read-only. No scraping Marketplace.
GMAIL_ENABLED = os.environ.get("GMAIL_ENABLED", "0").strip() == "1"
# Put the token JSON string in an env var (Railway-friendly)
GMAIL_TOKEN_JSON = os.environ.get("GMAIL_TOKEN_JSON", "").strip()
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_QUERY = os.environ.get(
    "GMAIL_QUERY",
    'from:facebookmail.com (marketplace OR subject:Marketplace) newer_than:14d'
).strip()

# Match Marketplace item URLs in email bodies
FB_MARKET_ITEM_RE = re.compile(r"https?://(?:www\.)?facebook\.com/marketplace/item/\d+", re.IGNORECASE)
# Facebook email links sometimes use redirectors
FB_REDIRECT_RE = re.compile(r"https?://l\.facebook\.com/l\.php\?[^ \n\r\t>]+", re.IGNORECASE)

PRICE_RE = re.compile(r"\$(\d+)", re.IGNORECASE)


# ==========================================================
# MODELS
# ==========================================================
@dataclass
class Listing:
    title: str
    link: str
    published: str = ""
    where: Optional[str] = None
    price: Optional[str] = None
    source: Optional[str] = None
    extra: Optional[str] = None


# ==========================================================
# DATABASE
# ==========================================================
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS guild_config (
            guild_id INTEGER PRIMARY KEY,
            zip TEXT NOT NULL,
            radius_miles INTEGER NOT NULL,
            category_id INTEGER,
            channel_local_id INTEGER,
            channel_online_id INTEGER,
            role_local_id INTEGER,
            role_online_id INTEGER,
            updated_ts INTEGER DEFAULT (strftime('%s','now'))
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_links (
            guild_id INTEGER NOT NULL,
            feed_type TEXT NOT NULL,
            link TEXT NOT NULL,
            first_seen_ts INTEGER DEFAULT (strftime('%s','now')),
            PRIMARY KEY (guild_id, feed_type, link)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS custom_rss (
            guild_id INTEGER NOT NULL,
            feed_url TEXT NOT NULL,
            kind TEXT NOT NULL, -- 'online'
            PRIMARY KEY (guild_id, feed_url)
        )
        """
    )

    conn.commit()
    conn.close()

def upsert_guild_config(
    guild_id: int,
    zip_code: str,
    radius_miles: int,
    category_id: int,
    channel_local_id: int,
    channel_online_id: int,
    role_local_id: int,
    role_online_id: int,
):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO guild_config (
            guild_id, zip, radius_miles, category_id,
            channel_local_id, channel_online_id,
            role_local_id, role_online_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET
            zip=excluded.zip,
            radius_miles=excluded.radius_miles,
            category_id=excluded.category_id,
            channel_local_id=excluded.channel_local_id,
            channel_online_id=excluded.channel_online_id,
            role_local_id=excluded.role_local_id,
            role_online_id=excluded.role_online_id,
            updated_ts=(strftime('%s','now'))
        """,
        (
            guild_id, zip_code, radius_miles, category_id,
            channel_local_id, channel_online_id,
            role_local_id, role_online_id
        )
    )
    conn.commit()
    conn.close()

def get_all_guild_configs() -> List[Dict[str, Any]]:
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT guild_id, zip, radius_miles, category_id,
               channel_local_id, channel_online_id,
               role_local_id, role_online_id
        FROM guild_config
        """
    )
    rows = cur.fetchall()
    conn.close()

    out = []
    for r in rows:
        out.append({
            "guild_id": int(r[0]),
            "zip": str(r[1]),
            "radius_miles": int(r[2]),
            "category_id": int(r[3]) if r[3] else None,
            "channel_local_id": int(r[4]) if r[4] else None,
            "channel_online_id": int(r[5]) if r[5] else None,
            "role_local_id": int(r[6]) if r[6] else None,
            "role_online_id": int(r[7]) if r[7] else None,
        })
    return out

def already_seen(guild_id: int, feed_type: str, link: str) -> bool:
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM seen_links WHERE guild_id=? AND feed_type=? AND link=?",
        (guild_id, feed_type, link)
    )
    row = cur.fetchone()
    conn.close()
    return row is not None

def mark_seen(guild_id: int, feed_type: str, link: str):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO seen_links (guild_id, feed_type, link) VALUES (?, ?, ?)",
        (guild_id, feed_type, link)
    )
    conn.commit()
    conn.close()

def add_custom_rss(guild_id: int, feed_url: str):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO custom_rss (guild_id, feed_url, kind) VALUES (?, ?, 'online')",
        (guild_id, feed_url)
    )
    conn.commit()
    conn.close()

def list_custom_rss(guild_id: int) -> List[str]:
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT feed_url FROM custom_rss WHERE guild_id=? AND kind='online' ORDER BY feed_url",
        (guild_id,)
    )
    rows = cur.fetchall()
    conn.close()
    return [r[0] for r in rows]


# ==========================================================
# FILTERS / EMBEDS
# ==========================================================
def extract_price(title: str) -> Optional[str]:
    m = PRICE_RE.search(title)
    if m:
        return f"${m.group(1)}"
    if "free" in title.lower():
        return "FREE"
    return None

def passes_filters(text: str) -> bool:
    t = text.lower()
    if EXCLUDE_KEYWORDS and any(k in t for k in EXCLUDE_KEYWORDS):
        return False
    if INCLUDE_KEYWORDS:
        return any(k in t for k in INCLUDE_KEYWORDS)
    return True

def make_embed(listing: Listing) -> discord.Embed:
    title = (listing.title or "Free item")[:256]
    embed = discord.Embed(title=title, url=listing.link)

    if listing.price:
        embed.add_field(name="Price", value=listing.price, inline=True)
    if listing.where:
        embed.add_field(name="Area", value=str(listing.where)[:1024], inline=True)
    if listing.source:
        embed.add_field(name="Source", value=str(listing.source)[:1024], inline=True)
    if listing.extra:
        embed.add_field(name="Details", value=str(listing.extra)[:1024], inline=False)
    if listing.published:
        embed.set_footer(text=f"Posted: {listing.published[:100]}")
    return embed


# ==========================================================
# FETCH HELPERS
# ==========================================================
async def fetch_text(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=25)) as resp:
        resp.raise_for_status()
        return await resp.text()

async def fetch_json(session: aiohttp.ClientSession, url: str) -> Any:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=25)) as resp:
        resp.raise_for_status()
        return await resp.json(content_type=None)

async def fetch_rss_listings(session: aiohttp.ClientSession, url: str, source_name: str) -> List[Listing]:
    data = await fetch_text(session, url)
    feed = feedparser.parse(data)

    out: List[Listing] = []
    for e in feed.entries:
        title = getattr(e, "title", "").strip()
        link = getattr(e, "link", "").strip()
        published = (getattr(e, "published", "") or getattr(e, "updated", "") or "").strip()
        summary = getattr(e, "summary", "") or getattr(e, "description", "") or ""
        summary = re.sub(r"\s+", " ", str(summary)).strip()

        out.append(Listing(
            title=title,
            link=link,
            published=published,
            price=extract_price(title),
            source=source_name,
            extra=(summary[:900] if summary else None)
        ))
    return out


# ==========================================================
# LOCAL: CRAIGSLIST
# ==========================================================
def craigslist_rss(zip_code: str, radius_miles: int) -> str:
    params = {"postal": zip_code, "search_distance": radius_miles, "format": "rss"}
    return f"{CRAIGSLIST_BASE}?{urlencode(params)}"

async def fetch_craigslist(session: aiohttp.ClientSession, zip_code: str, radius_miles: int) -> List[Listing]:
    url = craigslist_rss(zip_code, radius_miles)
    data = await fetch_text(session, url)
    feed = feedparser.parse(data)

    out: List[Listing] = []
    for e in feed.entries:
        title = getattr(e, "title", "").strip()
        link = getattr(e, "link", "").strip()
        published = getattr(e, "published", "").strip()

        where = None
        if hasattr(e, "where"):
            where = str(e.where)
        elif hasattr(e, "dc_source"):
            where = str(e.dc_source)

        out.append(Listing(
            title=title,
            link=link,
            published=published,
            where=where,
            price=extract_price(title),
            source="Craigslist (Free Stuff)",
        ))
    return out


# ==========================================================
# ONLINE: STEAMDB (best-effort)
# ==========================================================
async def fetch_steamdb_free_promos(session: aiohttp.ClientSession) -> List[Listing]:
    html = await fetch_text(session, STEAMDB_FREE_PROMOS_URL)
    store_links = re.findall(r'href="(https?://store\.steampowered\.com/app/\d+[^"]*)"', html)

    out: List[Listing] = []
    for link in dict.fromkeys(store_links):
        out.append(Listing(
            title="[Steam] Free promotion (SteamDB)",
            link=link,
            price="FREE",
            source="SteamDB Free Promotions",
        ))
    return out


# ==========================================================
# ONLINE: GAMERPOWER (Steam/Epic/GOG)
# ==========================================================
def gamerpower_url(platform: str) -> str:
    params = {"platform": platform, "type": "game", "sort-by": "date"}
    return f"{GAMERPOWER_API}/giveaways?{urlencode(params)}"

async def fetch_gamerpower(session: aiohttp.ClientSession, platform: str) -> List[Listing]:
    url = gamerpower_url(platform)
    data = await fetch_json(session, url)

    out: List[Listing] = []
    if not isinstance(data, list):
        return out

    for g in data:
        title = str(g.get("title", "")).strip()
        link = str(g.get("open_giveaway_url") or g.get("giveaway_url") or "").strip()
        end_date = str(g.get("end_date") or "").strip()
        worth = str(g.get("worth") or "").strip()
        platforms = str(g.get("platforms") or "").strip()

        if not title or not link:
            continue

        extra_bits = []
        if platforms:
            extra_bits.append(platforms)
        if worth:
            extra_bits.append(f"Worth: {worth}")
        if end_date and end_date.lower() != "n/a":
            extra_bits.append(f"Ends: {end_date}")

        out.append(Listing(
            title=f"[{platform}] {title}",
            link=link,
            source="GamerPower",
            price="FREE",
            extra=" | ".join(extra_bits) if extra_bits else None,
        ))
    return out


# ==========================================================
# ONLINE: REDDIT RSS
# ==========================================================
def reddit_rss(subreddit: str) -> str:
    return f"https://www.reddit.com/r/{subreddit}/.rss"

async def fetch_reddit_sub(session: aiohttp.ClientSession, subreddit: str) -> List[Listing]:
    return await fetch_rss_listings(session, reddit_rss(subreddit), source_name=f"Reddit r/{subreddit}")


# ==========================================================
# GMAIL: Facebook Marketplace notifications via Gmail API
# ==========================================================
def gmail_get_service() -> Optional[Any]:
    if not (GMAIL_ENABLED and GMAIL_TOKEN_JSON):
        return None
    try:
        token_info = json.loads(GMAIL_TOKEN_JSON)
        creds = Credentials.from_authorized_user_info(token_info, scopes=GMAIL_SCOPES)
    except Exception:
        return None

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            return None

    return build("gmail", "v1", credentials=creds, cache_discovery=False)

def gmail_extract_text(payload: Dict[str, Any]) -> str:
    parts: List[str] = []

    def walk(part: Dict[str, Any]):
        mime = part.get("mimeType", "")
        body = part.get("body", {}) or {}
        data = body.get("data")
        if data and mime in ("text/plain", "text/html"):
            try:
                decoded = base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="ignore")
                parts.append(decoded)
            except Exception:
                pass
        for p in part.get("parts", []) or []:
            walk(p)

    walk(payload)
    return "\n".join(parts)

def gmail_header(headers: List[Dict[str, str]], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""

def gmail_find_marketplace_link(text: str) -> Optional[str]:
    m = FB_MARKET_ITEM_RE.search(text)
    if m:
        return m.group(0)
    m2 = FB_REDIRECT_RE.search(text)
    if m2:
        return m2.group(0)
    return None

def gmail_fetch_marketplace_listings(service: Any, max_messages: int = 10) -> List[Listing]:
    results: List[Listing] = []
    try:
        resp = service.users().messages().list(
            userId="me",
            q=GMAIL_QUERY,
            maxResults=max_messages
        ).execute()
    except HttpError as e:
        print(f"[GMAIL] list error: {e}")
        return results

    msg_refs = resp.get("messages", []) or []
    for ref in msg_refs:
        msg_id = ref.get("id")
        if not msg_id:
            continue

        try:
            msg = service.users().messages().get(
                userId="me",
                id=msg_id,
                format="full"
            ).execute()
        except HttpError as e:
            print(f"[GMAIL] get error: {e}")
            continue

        payload = msg.get("payload", {}) or {}
        headers = payload.get("headers", []) or []
        subject = gmail_header(headers, "Subject") or "Facebook Marketplace alert"
        datev = gmail_header(headers, "Date") or ""

        text = gmail_extract_text(payload)
        link = gmail_find_marketplace_link(text)
        if not link:
            continue

        results.append(Listing(
            title=subject,
            link=link,
            published=datev,
            price="FREE?",
            source="Facebook Marketplace (Gmail alerts)",
        ))

    return results


# ==========================================================
# DISCORD BOT
# ==========================================================
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

def user_can_setup(interaction: discord.Interaction) -> bool:
    perms = interaction.user.guild_permissions
    return perms.manage_guild or perms.administrator

async def ensure_role(guild: discord.Guild, name: str) -> discord.Role:
    role = discord.utils.get(guild.roles, name=name)
    if role:
        return role
    return await guild.create_role(name=name, reason="Freebie Radar setup")

async def ensure_category(guild: discord.Guild, name: str) -> discord.CategoryChannel:
    cat = discord.utils.get(guild.categories, name=name)
    if cat:
        return cat
    return await guild.create_category(name=name, reason="Freebie Radar setup")

async def ensure_text_channel(guild: discord.Guild, category: discord.CategoryChannel, name: str, topic: str) -> discord.TextChannel:
    ch = discord.utils.get(guild.text_channels, name=name)
    if ch:
        if ch.category_id != category.id:
            await ch.edit(category=category, reason="Freebie Radar setup: move channel")
        if (ch.topic or "") != topic:
            await ch.edit(topic=topic, reason="Freebie Radar setup: update topic")
        return ch
    return await guild.create_text_channel(name=name, category=category, topic=topic, reason="Freebie Radar setup")

@bot.event
async def on_ready():
    init_db()
    await bot.tree.sync()
    print(f"Logged in as {bot.user} ({bot.user.id})")
    if not poll_local.is_running():
        poll_local.start()
    if not poll_online.is_running():
        poll_online.start()
    if GMAIL_ENABLED and not poll_gmail.is_running():
        poll_gmail.start()

@bot.tree.command(name="setup", description="Create Freebie Radar channels + ping roles.")
@app_commands.describe(zip_code="Zip code (default 95673)", radius_miles="Radius miles (default 20)")
async def setup(interaction: discord.Interaction, zip_code: Optional[str] = None, radius_miles: Optional[int] = None):
    if not interaction.guild:
        return await interaction.response.send_message("Run this in a server, not DMs.", ephemeral=True)
    if not user_can_setup(interaction):
        return await interaction.response.send_message("You need Manage Server (or Admin) to run setup.", ephemeral=True)

    me = interaction.guild.me
    if not me.guild_permissions.manage_channels or not me.guild_permissions.manage_roles:
        return await interaction.response.send_message("I need Manage Channels + Manage Roles to do setup.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)

    zip_code = (zip_code or DEFAULT_ZIP).strip()
    radius_miles = int(radius_miles or DEFAULT_RADIUS_MILES)

    local_role = await ensure_role(interaction.guild, ROLE_LOCAL)
    online_role = await ensure_role(interaction.guild, ROLE_ONLINE)
    category = await ensure_category(interaction.guild, SETUP_CATEGORY_NAME)

    created_channels: Dict[str, int] = {}
    for cname, topic in CHANNELS_TO_CREATE:
        ch = await ensure_text_channel(interaction.guild, category, cname, topic)
        created_channels[cname] = ch.id

    upsert_guild_config(
        guild_id=interaction.guild.id,
        zip_code=zip_code,
        radius_miles=radius_miles,
        category_id=category.id,
        channel_local_id=created_channels["free-local"],
        channel_online_id=created_channels["free-online"],
        role_local_id=local_role.id,
        role_online_id=online_role.id,
    )

    await interaction.followup.send(
        "Setup complete ✅\n"
        f"- Category: **{SETUP_CATEGORY_NAME}**\n"
        f"- Roles: **{ROLE_LOCAL}**, **{ROLE_ONLINE}**\n"
        "- Channels: **#free-local**, **#free-online**, **#requests**, **#claimed-and-dead**\n\n"
        "If pings don’t work: move the bot’s role above the Freebie Radar roles (Server Settings → Roles).",
        ephemeral=True
    )

@bot.tree.command(name="addrss", description="Add a custom RSS feed watched for online freebies.")
@app_commands.describe(feed_url="RSS feed URL")
async def addrss(interaction: discord.Interaction, feed_url: str):
    if not interaction.guild:
        return await interaction.response.send_message("Run this in a server, not DMs.", ephemeral=True)
    if not user_can_setup(interaction):
        return await interaction.response.send_message("You need Manage Server (or Admin) to add feeds.", ephemeral=True)

    feed_url = feed_url.strip()
    if not (feed_url.startswith("http://") or feed_url.startswith("https://")):
        return await interaction.response.send_message("That doesn’t look like a URL.", ephemeral=True)

    add_custom_rss(interaction.guild.id, feed_url)
    await interaction.response.send_message("Added ✅ (will post into #free-online)", ephemeral=True)

@bot.tree.command(name="listrss", description="List custom RSS feeds watched for online freebies.")
async def listrss(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Run this in a server, not DMs.", ephemeral=True)
    feeds = list_custom_rss(interaction.guild.id)
    if not feeds:
        return await interaction.response.send_message("No custom feeds added yet.", ephemeral=True)
    msg = "Custom RSS feeds:\n" + "\n".join(f"- {f}" for f in feeds[:25])
    if len(feeds) > 25:
        msg += f"\n…and {len(feeds) - 25} more."
    await interaction.response.send_message(msg, ephemeral=True)


# ==========================================================
# POLLERS
# ==========================================================
@tasks.loop(seconds=POLL_SECONDS_LOCAL)
async def poll_local():
    configs = get_all_guild_configs()
    if not configs:
        return

    try:
        async with aiohttp.ClientSession(headers={"User-Agent": "VioletFreebieRadar/1.0"}) as session:
            for cfg in configs:
                guild_id = cfg["guild_id"]
                channel_id = cfg["channel_local_id"]
                role_id = cfg["role_local_id"]

                channel = bot.get_channel(channel_id) if channel_id else None
                if not isinstance(channel, discord.TextChannel):
                    continue

                listings = await fetch_craigslist(session, cfg["zip"], cfg["radius_miles"])
                for item in listings:
                    if not item.link:
                        continue
                    if already_seen(guild_id, "craigslist_local", item.link):
                        continue
                    if not passes_filters(f"{item.title} {item.where or ''}"):
                        mark_seen(guild_id, "craigslist_local", item.link)
                        continue

                    await channel.send(
                        content=(f"<@&{role_id}>" if role_id else ""),
                        embed=make_embed(item),
                    )
                    mark_seen(guild_id, "craigslist_local", item.link)
                    await asyncio.sleep(1.0)
    except Exception as e:
        print(f"[LOCAL] error: {e}")

@tasks.loop(seconds=POLL_SECONDS_ONLINE)
async def poll_online():
    configs = get_all_guild_configs()
    if not configs:
        return

    try:
        async with aiohttp.ClientSession(headers={"User-Agent": "VioletFreebieRadar/1.0"}) as session:
            gp_steam = await fetch_gamerpower(session, "steam")
            gp_epic = await fetch_gamerpower(session, "epic-games-store")
            gp_gog = await fetch_gamerpower(session, "gog")
            steamdb = await fetch_steamdb_free_promos(session)

            reddit_items: List[Listing] = []
            for sub in DEFAULT_REDDIT_SUBS:
                try:
                    reddit_items.extend(await fetch_reddit_sub(session, sub))
                    await asyncio.sleep(0.2)
                except Exception:
                    pass

            tech_items: List[Listing] = []
            for feed_url in DEFAULT_TECH_RSS:
                try:
                    tech_items.extend(await fetch_rss_listings(session, feed_url, source_name="Tech/Freebies RSS"))
                    await asyncio.sleep(0.2)
                except Exception:
                    pass

            for cfg in configs:
                guild_id = cfg["guild_id"]
                channel_id = cfg["channel_online_id"]
                role_id = cfg["role_online_id"]

                channel = bot.get_channel(channel_id) if channel_id else None
                if not isinstance(channel, discord.TextChannel):
                    continue

                custom_items: List[Listing] = []
                for feed_url in list_custom_rss(guild_id):
                    try:
                        custom_items.extend(await fetch_rss_listings(session, feed_url, source_name="Custom RSS"))
                        await asyncio.sleep(0.2)
                    except Exception:
                        pass

                buckets = [
                    ("gp_steam", gp_steam),
                    ("gp_epic", gp_epic),
                    ("gp_gog", gp_gog),
                    ("steamdb_free", steamdb),
                    ("reddit", reddit_items),
                    ("tech_rss", tech_items),
                    ("custom_rss", custom_items),
                ]

                for feed_type, items in buckets:
                    for item in items:
                        if not item.link:
                            continue
                        if already_seen(guild_id, feed_type, item.link):
                            continue
                        if not passes_filters(f"{item.title} {item.extra or ''} {item.source or ''}"):
                            mark_seen(guild_id, feed_type, item.link)
                            continue

                        await channel.send(
                            content=(f"<@&{role_id}>" if role_id else ""),
                            embed=make_embed(item),
                        )
                        mark_seen(guild_id, feed_type, item.link)
                        await asyncio.sleep(0.8)
    except Exception as e:
        print(f"[ONLINE] error: {e}")

@tasks.loop(seconds=POLL_SECONDS_GMAIL)
async def poll_gmail():
    if not GMAIL_ENABLED:
        return

    service = gmail_get_service()
    if service is None:
        print("[GMAIL] Missing/invalid GMAIL_TOKEN_JSON or GMAIL_ENABLED not set.")
        return

    configs = get_all_guild_configs()
    if not configs:
        return

    try:
        items = gmail_fetch_marketplace_listings(service, max_messages=10)
        if not items:
            return

        for cfg in configs:
            guild_id = cfg["guild_id"]
            channel_id = cfg["channel_local_id"]
            role_id = cfg["role_local_id"]

            channel = bot.get_channel(channel_id) if channel_id else None
            if not isinstance(channel, discord.TextChannel):
                continue

            for item in items:
                if already_seen(guild_id, "facebook_gmail", item.link):
                    continue

                if not passes_filters(item.title):
                    mark_seen(guild_id, "facebook_gmail", item.link)
                    continue

                await channel.send(
                    content=(f"<@&{role_id}>" if role_id else ""),
                    embed=make_embed(item),
                )
                mark_seen(guild_id, "facebook_gmail", item.link)
                await asyncio.sleep(0.8)

    except Exception as e:
        print(f"[GMAIL] error: {e}")


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN env var.")
    bot.run(DISCORD_TOKEN)
