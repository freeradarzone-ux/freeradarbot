# Free Radar (Discord Bot) - V2 Upgrade

Free Radar posts:
- **Local pickups:** Craigslist free section (ZIP + radius)
- **Online freebies:** RSS feeds (Reddit/Humble/Itch/Slickdeals/ProductHunt + AI subs) + Epic free promos JSON
- **Optional:** Facebook Marketplace alerts via **Gmail notifications** (no scraping)

## Deploy (GitHub + Railway)
1. Upload these files to a GitHub repo
2. Railway -> New Project -> Deploy from GitHub
3. Add Railway Variables:
   - DISCORD_TOKEN = your bot token
   - ZIP = 95673
   - RADIUS_MILES = 20

Included:
- `Procfile` -> Railway worker starts `python bot.py`
- `runtime.txt` -> pins Python 3.12 to avoid Python 3.13 `audioop` removal issue.

## Discord (first run)
- `/setup` creates channels + ping roles
- `/scan` triggers a scan immediately

Roles created (mentionable):
- Free Radar Local
- Free Radar Online

Channels created under category "Free Radar":
- #free-local
- #free-games
- #free-software
- #ai-tools
- #requests
- #claimed-and-dead

## Watchlist (per server)
- `/watch couch`
- `/watch bike`
- `/watch laptop`
- `/watchlist`
- `/unwatch couch`

Watch hits add an extra 🔔 next to the ping.

## Optional Gmail Marketplace ingestion
This reads your Gmail inbox for Facebook Marketplace notification emails.
It does NOT scrape Facebook.

### Steps
1) Google Cloud Console -> create project -> enable Gmail API  
2) Create OAuth Client ID (Desktop app) and download JSON  
3) Save it next to `make_token.py` as `oauth_client.json`  
4) Run: `python make_token.py`  
5) Paste output into Railway variable `GMAIL_TOKEN_JSON`  
6) Set Railway variable: `GMAIL_ENABLED=1`  
7) (Optional) customize `GMAIL_QUERY`

Default query:
`from:facebookmail.com (marketplace OR subject:Marketplace) newer_than:14d`
