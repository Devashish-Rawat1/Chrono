"""
Web-based Google OAuth for the hosted Chrono server.

The desktop app uses InstalledAppFlow.run_local_server() (a loopback flow),
which only works when the browser and the Python process are on the same
machine. A hosted server can't do that, so this module implements the
standard web redirect flow instead:

  1. /auth/google/login  -> build an auth URL, redirect the user to Google
  2. user signs in & consents on Google
  3. Google redirects back to /auth/google/callback?code=...
  4. we exchange the code for credentials and store them PER SESSION

Credentials live per browser session (keyed by the same cookie the API
uses), so each visitor connects their own Google account and only touches
their own calendar.

Configuration comes from environment variables (never committed):
  GOOGLE_CLIENT_ID       - OAuth client id
  GOOGLE_CLIENT_SECRET   - OAuth client secret
  GOOGLE_REDIRECT_URI    - e.g. https://your-app.onrender.com/auth/google/callback
  OAUTHLIB_INSECURE_TRANSPORT=1  (ONLY for local http testing; omit in prod)
"""

import os
import json

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
]


def _client_config() -> dict:
    """Builds the OAuth client config from env vars (no file on disk)."""
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    redirect_uri = os.environ.get("GOOGLE_REDIRECT_URI")
    if not (client_id and client_secret and redirect_uri):
        raise RuntimeError(
            "Google OAuth is not configured. Set GOOGLE_CLIENT_ID, "
            "GOOGLE_CLIENT_SECRET, and GOOGLE_REDIRECT_URI environment "
            "variables on the host."
        )
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }


def is_configured() -> bool:
    """True if the server has the env vars needed for Google OAuth."""
    return all(
        os.environ.get(k)
        for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI")
    )


def build_auth_url() -> tuple[str, str]:
    """
    Returns (authorization_url, state). Redirect the user to the URL;
    keep the state to verify the callback.
    """
    flow = Flow.from_client_config(_client_config(), scopes=SCOPES)
    flow.redirect_uri = os.environ["GOOGLE_REDIRECT_URI"]
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return auth_url, state


def exchange_code(code: str) -> dict:
    """
    Exchanges the callback `code` for credentials, returned as a JSON-able
    dict we can stash in the session (and later rebuild into Credentials).
    """
    flow = Flow.from_client_config(_client_config(), scopes=SCOPES)
    flow.redirect_uri = os.environ["GOOGLE_REDIRECT_URI"]
    flow.fetch_token(code=code)
    creds = flow.credentials
    return json.loads(creds.to_json())


def credentials_from_session(token_dict: dict) -> Credentials:
    """Rebuilds a Credentials object from the stored session token dict."""
    return Credentials.from_authorized_user_info(token_dict, SCOPES)


def service_from_session(token_dict: dict):
    """Builds a Calendar API service from stored session credentials."""
    creds = credentials_from_session(token_dict)
    return build("calendar", "v3", credentials=creds)


def has_valid_session(token_dict) -> bool:
    """True if the session holds usable (non-expired-without-refresh) creds."""
    if not token_dict:
        return False
    try:
        creds = credentials_from_session(token_dict)
        return bool(creds and (creds.valid or creds.refresh_token))
    except Exception:
        return False