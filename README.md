# Violet Freebie Radar (Discord Bot)

All-in-one Discord bot that posts:
- Local freebies: Craigslist Free Stuff near a ZIP within a radius
- Online freebies: Steam/Epic/GOG (GamerPower), Steam promos (SteamDB best-effort), Reddit RSS, Tech/Freebies RSS
- Optional: Facebook Marketplace alerts via Gmail API (reads your Marketplace notification emails)

## 1) Create your Discord Bot + Token
1. Discord Developer Portal -> New Application
2. Bot -> Add Bot -> Reset/Copy Token
3. OAuth2 -> URL Generator:
   - Scopes: bot, applications.commands
   - Bot Permissions: Manage Channels, Manage Roles, Send Messages, Embed Links
4. Invite bot to your server.
5. Server Settings -> Roles -> move the bot's role ABOVE the Freebie Radar roles.

## 2) GitHub
Create a repo and upload these files:
- bot.py
- requirements.txt
- make_token.py
- README.md
- .gitignore

## 3) Railway Deploy
Railway -> New Project -> Deploy from GitHub Repo -> select your repo

Railway -> Variables:
- DISCORD_TOKEN = your bot token
- ZIP = 95673
- RADIUS_MILES = 20

Optional filters:
- INCLUDE_KEYWORDS = comma,separated,keywords
- EXCLUDE_KEYWORDS = comma,separated,keywords

## 4) Discord Setup
After deploy, in your Discord server run:
- /setup

Creates:
- Category: Freebie Radar
- Channels: #free-local, #free-online, #requests, #claimed-and-dead
- Roles: Freebie Radar Local, Freebie Radar Online

## 5) Optional Gmail + Facebook Marketplace Alerts (Safe / Official)
This does NOT scrape Marketplace. It reads your Facebook Marketplace notification emails in Gmail via Gmail API.

Facebook:
- Enable Marketplace email notifications
- Save searches so emails are sent

Gmail API token (one-time on your PC):
1) In Google Cloud Console:
   - Enable Gmail API
   - Create OAuth Client ID: Desktop app
   - Download JSON as oauth_client.json next to make_token.py
2) Run locally:
   - pip install -r requirements.txt
   - python make_token.py
3) Set Railway Variables:
   - GMAIL_ENABLED = 1
   - GMAIL_TOKEN_JSON = (paste the printed JSON)
   - GMAIL_QUERY = from:facebookmail.com (marketplace OR subject:Marketplace) newer_than:14d

Redeploy. Marketplace alerts will post into #free-local.
