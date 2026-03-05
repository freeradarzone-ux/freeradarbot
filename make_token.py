#!/usr/bin/env python3
"""
Generate GMAIL_TOKEN_JSON for Free Radar.

1) Google Cloud Console -> Enable Gmail API
2) Create OAuth Client ID (Desktop app)
3) Download JSON and save as oauth_client.json in this folder
4) Run: python make_token.py
5) Paste the printed JSON into Railway variable GMAIL_TOKEN_JSON
"""
import json
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

def main():
    flow = InstalledAppFlow.from_client_secrets_file("oauth_client.json", SCOPES)
    creds = flow.run_local_server(port=0)
    token = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    }
    print(json.dumps(token))
    print("\nPaste the JSON above into Railway as GMAIL_TOKEN_JSON.\n")

if __name__ == "__main__":
    main()
