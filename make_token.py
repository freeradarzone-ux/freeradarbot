from google_auth_oauthlib.flow import InstalledAppFlow

# Create token JSON for Gmail read-only access.
# 1) Put your OAuth Desktop credentials file as oauth_client.json in this folder.
# 2) Run: python make_token.py
# 3) Copy/paste the printed JSON into Railway variable: GMAIL_TOKEN_JSON

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

flow = InstalledAppFlow.from_client_secrets_file("oauth_client.json", SCOPES)
creds = flow.run_local_server(port=0)
print(creds.to_json())
