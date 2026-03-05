#!/usr/bin/env python3
"""
Free Radar (Discord bot)
- Posts local freebies (Craigslist RSS with postal + radius)
- Posts online freebies (RSS feeds + Epic free promos JSON)
- Optional: Gmail ingestion for Facebook Marketplace notification emails (no scraping)

Railway friendly:
- Procfile worker: python bot.py
- runtime.txt pins Python 3.12 (discord.py + Python 3.13 audioop removal issue)
"""
from __future__ import annotations

import os
import re
import json
import time
import asyncio
import hashlib
import logging
import sqlite3
from dataclasses import dataclass
from typing import List, Dict, Tuple

import aiohttp
import feedparser
import discord
from discord import app_commands

# ----------------------------
# Config (env)
# ----------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

ZIP = os.getenv("ZIP", "95673").strip()
RADIUS_MILES = int((os.getenv("RADIUS_MILES", "20").strip() or "20"))

# Optional keyword filters (global; watchlist is per-server)
INCLUDE_KEYWORDS = [k.strip().lower() for k in os.getenv("INCLUDE_KEYWORDS", "").split(",") if k.strip()]
EXCLUDE_KEYWORDS = [k.strip().lower() for k in os.getenv("EXCLUDE_KEYWORDS", "").split(",") if k.strip()]

# Scheduling (seconds)
LOCAL_INTERVAL = int(os.getenv("LOCAL_INTERVAL", "180"))   # 3 min
ONLINE_INTERVAL = int(os.getenv("ONLINE_INTERVAL", "300")) # 5 min
GMAIL_INTERVAL = int(os.getenv("GMAIL_INTERVAL", "180"))   # 3 min

# Optional Gmail ingestion
GMAIL_ENABLED = os.getenv("GMAIL_ENABLED", "0").strip() == "1"
GMAIL_TOKEN_JSON = os.getenv("GMAIL_TOKEN_JSON", "").strip()
GMAIL_QUERY = os.getenv(
    "GMAIL_QUERY",
    "from:facebookmail.com (marketplace OR subject:Marketplace) newer_than:14d"
).strip()

# Discord structure
CATEGORY_NAME = "Free Radar"

CH_FREE_LOCAL = "free-local"
CH_FREE_GAMES = "free-games"
CH_FREE_SOFTWARE = "free-software"
CH_AI_TOOLS = "ai-tools"
CH_REQUESTS = "requests"
CH_CLAIMED = "claimed-and-dead"

ROLE_LOCAL = "Free Radar Local"
ROLE_ONLINE = "Free Radar Online"

DB_PATH = os.getenv("DB_PATH", "data.db").strip()

# Craigslist region (change if you want)
CRAIGSLIST_SITE = os.getenv("CRAIGSLIST_SITE", "sacramento").strip()

# Epic free games JSON endpoint
EPIC_FREE_GAMES_JSON = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions?locale=en-US&country=US&allowCountries=US"

# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("free-radar")

# ----------------------------
# Data model
# ----------------------------
@dataclass
class RadarItem:
    title: str
    url: str
    source: str
    channel_key: str  # local, games, software, ai
    summary: str = ""
    score: int = 0

# ----------------------------
# Database (sqlite)
# ----------------------------
def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("""
        CREATE TABLE IF NOT EXISTS posted (
            guild_id TEXT NOT NULL,
            url_hash TEXT NOT NULL,
            url TEXT NOT NULL,
            created_ts INTEGER NOT NULL,
            PRIMARY KEY (guild_id, url_hash)
        );
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS watch (
            guild_id TEXT NOT NULL,
            keyword TEXT NOT NULL,
            created_ts INTEGER NOT NULL,
            PRIMARY KEY (guild_id, keyword)
        );
    """)
    con.commit()
    return con

def url_hash(url: str) -> str:
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()

def already_posted(guild_id: int, url: str) -> bool:
    h = url_hash(url)
    with db() as con:
        cur = con.execute("SELECT 1 FROM posted WHERE guild_id=? AND url_hash=? LIMIT 1", (str(guild_id), h))
        return cur.fetchone() is not None

def mark_posted(guild_id: int, url: str) -> None:
    h = url_hash(url)
    with db() as con:
        con.execute(
            "INSERT OR IGNORE INTO posted (guild_id, url_hash, url, created_ts) VALUES (?,?,?,?)",
            (str(guild_id), h, url, int(time.time()))
        )
        con.commit()

def get_watchlist(guild_id: int) -> List[str]:
    with db() as con:
        cur = con.execute("SELECT keyword FROM watch WHERE guild_id=? ORDER BY keyword", (str(guild_id),))
        return [r[0] for r in cur.fetchall()]

def add_watch(guild_id: int, keyword: str) -> None:
    keyword = keyword.strip().lower()
    if not keyword:
        return
    with db() as con:
        con.execute(
            "INSERT OR IGNORE INTO watch (guild_id, keyword, created_ts) VALUES (?,?,?)",
            (str(guild_id), keyword, int(time.time()))
        )
        con.commit()

def remove_watch(guild_id: int, keyword: str) -> bool:
    keyword = keyword.strip().lower()
    with db() as con:
        cur = con.execute("DELETE FROM watch WHERE guild_id=? AND keyword=?", (str(guild_id), keyword))
        con.commit()
        return cur.rowcount > 0

# ----------------------------
# Filtering + scoring
# ----------------------------
def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def passes_global_filters(text: str) -> bool:
    t = text.lower()
    if INCLUDE_KEYWORDS and not any(k in t for k in INCLUDE_KEYWORDS):
        return False
    if EXCLUDE_KEYWORDS and any(k in t for k in EXCLUDE_KEYWORDS):
        return False
    return True

def compute_score(title: str, summary: str) -> int:
    t = (title + " " + summary).lower()
    score = 0
    if "free" in t:
        score += 20
    if any(w in t for w in ["brand new", "sealed", "unused", "new in box", "nib"]):
        score += 10
    if any(w in t for w in ["gaming", "laptop", "pc", "monitor", "iphone", "ipad", "ps5", "xbox", "switch"]):
        score += 8
    if any(w in t for w in ["couch", "sofa", "dresser", "table", "chair", "desk", "bed"]):
        score += 6
    if any(w in t for w in ["iso", "wanted", "looking for", "trade", "swap"]):
        score -= 20
    if any(w in t for w in ["broken", "for parts", "not working", "junk"]):
        score -= 12
    if "$" in t:
        score -= 4
    return score

def watch_hit(guild_id: int, item: RadarItem) -> bool:
    wl = get_watchlist(guild_id)
    if not wl:
        return False
    hay = (item.title + " " + item.summary).lower()
    return any(k in hay for k in wl)

# ----------------------------
# Sources
# ----------------------------
def craigslist_free_rss(postal: str, radius: int) -> str:
    return f"https://{CRAIGSLIST_SITE}.craigslist.org/search/zip?postal={postal}&search_distance={radius}&format=rss"

def rss_sources() -> List[Tuple[str, str, str]]:
    # (name, url, channel_key)
    return [
        ("Reddit: FreeGameFindings", "https://www.reddit.com/r/FreeGameFindings/.rss", "games"),
        ("Reddit: GameDeals", "https://www.reddit.com/r/GameDeals/.rss", "games"),
        ("Reddit: freebies", "https://www.reddit.com/r/freebies/.rss", "software"),
        ("Reddit: software", "https://www.reddit.com/r/software/.rss", "software"),
        ("Humble Bundle", "https://www.humblebundle.com/rss", "games"),
        ("Itch.io Bundles", "https://itch.io/bundles/rss", "games"),
        ("Slickdeals Freebies", "https://slickdeals.net/freebies/rss", "software"),
        ("Product Hunt", "https://www.producthunt.com/feed", "software"),
        ("r/MachineLearning (for AI tools posts)", "https://www.reddit.com/r/MachineLearning/.rss", "ai"),
        ("r/ArtificialInteligence", "https://www.reddit.com/r/ArtificialInteligence/.rss", "ai"),
    ]

HEADERS = {
    "User-Agent": "FreeRadarBot/2.0 (+https://example.invalid)",
    "Accept": "*/*",
}

async def fetch_text(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as resp:
        resp.raise_for_status()
        return await resp.text()

async def fetch_json(session: aiohttp.ClientSession, url: str) -> dict:
    async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as resp:
        resp.raise_for_status()
        return await resp.json()

async def parse_rss(session: aiohttp.ClientSession, name: str, url: str, channel_key: str) -> List[RadarItem]:
    items: List[RadarItem] = []
    try:
        txt = await fetch_text(session, url)
        feed = feedparser.parse(txt)
        for e in feed.entries[:30]:
            title = normalize(getattr(e, "title", "") or "")
            link = normalize(getattr(e, "link", "") or "")
            summary = normalize(getattr(e, "summary", "") or "")
            if not title or not link:
                continue
            combo = f"{title} {summary}"
            if not passes_global_filters(combo):
                continue
            items.append(RadarItem(
                title=title,
                url=link,
                source=name,
                channel_key=channel_key,
                summary=summary[:400],
                score=compute_score(title, summary),
            ))
    except Exception as ex:
        log.warning("RSS failed: %s (%s)", name, ex)
    return items

async def epic_free_games(session: aiohttp.ClientSession) -> List[RadarItem]:
    items: List[RadarItem] = []
    try:
        data = await fetch_json(session, EPIC_FREE_GAMES_JSON)
        elements = (((data or {}).get("data") or {}).get("Catalog") or {}).get("searchStore", {}).get("elements", [])
        for el in elements:
            promos = el.get("promotions")
            if not promos:
                continue
            if not promos.get("promotionalOffers"):
                continue
            title = normalize(el.get("title", "") or "")
            if not title:
                continue
            slug = None
            for m in (el.get("catalogNs", {}).get("mappings") or []):
                if m.get("pageType") == "productHome":
                    slug = m.get("pageSlug")
                    break
            url = f"https://store.epicgames.com/p/{slug}" if slug else "https://store.epicgames.com/free-games"
            summary = "Epic Games Store free promotion"
            combo = f"{title} {summary}"
            if not passes_global_filters(combo):
                continue
            items.append(RadarItem(
                title=f"{title} (Epic Free)",
                url=url,
                source="Epic Games",
                channel_key="games",
                summary=summary,
                score=compute_score(title, summary) + 10,
            ))
    except Exception as ex:
        log.warning("Epic fetch failed: %s", ex)
    return items

# ----------------------------
# Gmail ingestion (optional)
# ----------------------------
def gmail_enabled() -> bool:
    return GMAIL_ENABLED and bool(GMAIL_TOKEN_JSON)

def gmail_marketplace_items_sync() -> List[RadarItem]:
    if not gmail_enabled():
        return []
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        token = json.loads(GMAIL_TOKEN_JSON)
        creds = Credentials.from_authorized_user_info(token)
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)

        resp = service.users().messages().list(userId="me", q=GMAIL_QUERY, maxResults=20).execute()
        msgs = resp.get("messages", []) or []

        out: List[RadarItem] = []
        for m in msgs:
            mid = m.get("id")
            if not mid:
                continue
            msg = service.users().messages().get(userId="me", id=mid, format="metadata").execute()
            headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", []) if "name" in h and "value" in h}
            subject = normalize(headers.get("subject", "Facebook Marketplace alert"))
            snippet = normalize(msg.get("snippet", ""))

            # Extract first Marketplace-ish URL from snippet (safe, avoids fetching full body)
            found = re.findall(r"https?://\S+", snippet)
            url = ""
            for u in found:
                u = u.strip(").,;]")
                if "facebook.com" in u and ("marketplace" in u or "fb.me" in u):
                    url = u
                    break
            if not url:
                continue

            combo = f"{subject} {snippet}"
            if not passes_global_filters(combo):
                continue

            out.append(RadarItem(
                title=subject,
                url=url,
                source="Facebook (via Gmail)",
                channel_key="local",
                summary=snippet[:400],
                score=compute_score(subject, snippet) + 5,
            ))
        return out
    except Exception as ex:
        log.warning("Gmail ingestion failed: %s", ex)
        return []

# ----------------------------
# Discord bot
# ----------------------------
INTENTS = discord.Intents.default()
client = discord.Client(intents=INTENTS)
tree = app_commands.CommandTree(client)

@client.event
async def setup_hook():
    # discord.py v2: start background tasks here (safe async init hook)
    client.radar_task = asyncio.create_task(radar_loop())


async def ensure_category_and_channels(guild: discord.Guild) -> Dict[str, discord.TextChannel]:
    category = discord.utils.get(guild.categories, name=CATEGORY_NAME)
    if category is None:
        category = await guild.create_category(CATEGORY_NAME, reason="Free Radar setup")

    async def get_or_create(name: str) -> discord.TextChannel:
        ch = discord.utils.get(guild.text_channels, name=name, category=category)
        if ch:
            return ch
        return await guild.create_text_channel(name, category=category, reason="Free Radar setup")

    return {
        "local": await get_or_create(CH_FREE_LOCAL),
        "games": await get_or_create(CH_FREE_GAMES),
        "software": await get_or_create(CH_FREE_SOFTWARE),
        "ai": await get_or_create(CH_AI_TOOLS),
        "requests": await get_or_create(CH_REQUESTS),
        "claimed": await get_or_create(CH_CLAIMED),
    }

async def ensure_roles(guild: discord.Guild) -> Dict[str, discord.Role]:
    roles: Dict[str, discord.Role] = {}
    for name in (ROLE_LOCAL, ROLE_ONLINE):
        r = discord.utils.get(guild.roles, name=name)
        if r is None:
            r = await guild.create_role(name=name, mentionable=True, reason="Free Radar setup")
        roles[name] = r
    return roles

def channel_for_item(channels: Dict[str, discord.TextChannel], item: RadarItem) -> discord.TextChannel:
    return channels.get(item.channel_key, channels["software"])

def role_mention_for_item(roles: Dict[str, discord.Role], item: RadarItem) -> str:
    return roles[ROLE_LOCAL].mention if item.channel_key == "local" else roles[ROLE_ONLINE].mention

def build_embed(item: RadarItem) -> discord.Embed:
    emb = discord.Embed(title=item.title[:250], url=item.url, description=(item.summary or "")[:2048])
    emb.add_field(name="Source", value=item.source, inline=True)
    emb.add_field(name="Score", value=str(item.score), inline=True)
    return emb

async def post_items(guild: discord.Guild, channels: Dict[str, discord.TextChannel], roles: Dict[str, discord.Role], items: List[RadarItem]) -> int:
    posted = 0
    items = sorted(items, key=lambda x: (-(x.score), x.title.lower()))
    for it in items:
        if already_posted(guild.id, it.url):
            continue
        try:
            ping = role_mention_for_item(roles, it)
            if watch_hit(guild.id, it):
                ping += " 🔔"
            await channel_for_item(channels, it).send(content=ping, embed=build_embed(it))
            mark_posted(guild.id, it.url)
            posted += 1
            await asyncio.sleep(1.2)
        except Exception as ex:
            log.warning("Posting failed (%s): %s", guild.name, ex)
    return posted

async def gather_all_items(session: aiohttp.ClientSession) -> List[RadarItem]:
    tasks = []

    # Local (Craigslist free section)
    tasks.append(parse_rss(session, "Craigslist Free", craigslist_free_rss(ZIP, RADIUS_MILES), "local"))

    # Online (RSS feeds)
    for name, url, key in rss_sources():
        tasks.append(parse_rss(session, name, url, key))

    # Online (Epic)
    tasks.append(epic_free_games(session))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    items: List[RadarItem] = []
    for r in results:
        if isinstance(r, Exception):
            continue
        items.extend(r)

    # Gmail (optional, blocking)
    if gmail_enabled():
        items.extend(await asyncio.to_thread(gmail_marketplace_items_sync))

    # online sanity filter: prefer explicit free/giveaway cues
    filtered: List[RadarItem] = []
    for it in items:
        text = (it.title + " " + it.summary).lower()
        if it.channel_key == "local":
            filtered.append(it)
        else:
            if any(k in text for k in ["free", "giveaway", "100% off", "claim", "free to keep", "limited time free"]):
                filtered.append(it)
    return filtered

async def radar_loop():
    await client.wait_until_ready()
    log.info("Free Radar online as %s", client.user)

    async with aiohttp.ClientSession() as session:
        last_local = 0.0
        last_online = 0.0
        last_gmail = 0.0

        while not client.is_closed():
            now = time.time()
            try:
                if (now - last_online) >= ONLINE_INTERVAL:
                    last_online = now
                    items = await gather_all_items(session)
                    for guild in client.guilds:
                        channels = await ensure_category_and_channels(guild)
                        roles = await ensure_roles(guild)
                        n = await post_items(guild, channels, roles, items)
                        if n:
                            log.info("Posted %s item(s) to %s", n, guild.name)

                # local heartbeat
                if (now - last_local) >= LOCAL_INTERVAL:
                    last_local = now
                    local_items = await parse_rss(session, "Craigslist Free", craigslist_free_rss(ZIP, RADIUS_MILES), "local")
                    for guild in client.guilds:
                        channels = await ensure_category_and_channels(guild)
                        roles = await ensure_roles(guild)
                        n = await post_items(guild, channels, roles, local_items)
                        if n:
                            log.info("Posted %s local item(s) to %s", n, guild.name)

                # gmail heartbeat (optional)
                if gmail_enabled() and (now - last_gmail) >= GMAIL_INTERVAL:
                    last_gmail = now
                    gmail_items = await asyncio.to_thread(gmail_marketplace_items_sync)
                    for guild in client.guilds:
                        channels = await ensure_category_and_channels(guild)
                        roles = await ensure_roles(guild)
                        n = await post_items(guild, channels, roles, gmail_items)
                        if n:
                            log.info("Posted %s Gmail item(s) to %s", n, guild.name)

            except Exception as ex:
                log.warning("Loop error: %s", ex)

            await asyncio.sleep(10)

# ----------------------------
# Slash commands
# ----------------------------
@tree.command(name="setup", description="Create Free Radar channels + ping roles in this server.")
async def setup(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Run this in a server, not DMs.", ephemeral=True)
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("You need Manage Server to run setup.", ephemeral=True)

    await interaction.response.defer(ephemeral=True, thinking=True)
    await ensure_category_and_channels(interaction.guild)
    await ensure_roles(interaction.guild)
    await interaction.followup.send("✅ Free Radar is set up. Channels + roles created.", ephemeral=True)

@tree.command(name="scan", description="Run a manual scan now (posts new items).")
async def scan(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Run this in a server, not DMs.", ephemeral=True)

    await interaction.response.defer(ephemeral=True, thinking=True)
    async with aiohttp.ClientSession() as session:
        items = await gather_all_items(session)
    channels = await ensure_category_and_channels(interaction.guild)
    roles = await ensure_roles(interaction.guild)
    n = await post_items(interaction.guild, channels, roles, items)
    await interaction.followup.send(f"📡 Scan complete. Posted {n} new item(s).", ephemeral=True)

@tree.command(name="watch", description="Add a keyword to this server's watchlist (adds 🔔).")
@app_commands.describe(keyword="Example: couch, bike, laptop, server, ps5")
async def watch(interaction: discord.Interaction, keyword: str):
    if not interaction.guild:
        return await interaction.response.send_message("Run this in a server, not DMs.", ephemeral=True)
    kw = keyword.strip().lower()
    if len(kw) < 2:
        return await interaction.response.send_message("Keyword too short.", ephemeral=True)
    add_watch(interaction.guild.id, kw)
    await interaction.response.send_message(f"✅ Added watch keyword: **{kw}**", ephemeral=True)

@tree.command(name="unwatch", description="Remove a keyword from this server's watchlist.")
@app_commands.describe(keyword="Keyword to remove")
async def unwatch(interaction: discord.Interaction, keyword: str):
    if not interaction.guild:
        return await interaction.response.send_message("Run this in a server, not DMs.", ephemeral=True)
    kw = keyword.strip().lower()
    ok = remove_watch(interaction.guild.id, kw)
    await interaction.response.send_message(
        (f"🗑️ Removed watch keyword: **{kw}**" if ok else f"Couldn't find **{kw}** in the watchlist."),
        ephemeral=True
    )

@tree.command(name="watchlist", description="Show this server's watchlist.")
async def watchlist(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Run this in a server, not DMs.", ephemeral=True)
    wl = get_watchlist(interaction.guild.id)
    if not wl:
        return await interaction.response.send_message("No watch keywords yet. Use `/watch`.", ephemeral=True)
    await interaction.response.send_message("🔎 Watchlist:\n" + "\n".join(f"- {k}" for k in wl), ephemeral=True)

@client.event
async def on_ready():
    try:
        await tree.sync()
        log.info("Slash commands synced.")
    except Exception as ex:
        log.warning("Slash command sync failed: %s", ex)

def main():
    if not DISCORD_TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN env var.")
    _ = db()
    client.run(DISCORD_TOKEN)

if __name__ == "__main__":
    main()