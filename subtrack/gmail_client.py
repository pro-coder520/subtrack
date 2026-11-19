"""Gmail OAuth and API client.

Handles the OAuth 2.0 flow for a Desktop app and provides helpers
to build a Gmail service and fetch the authenticated user's profile.
"""

from __future__ import annotations

import sys
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build, Resource

from subtrack.config import CREDENTIALS_PATH, TOKEN_PATH, ensure_data_dir

# gmail.readonly — the only scope we ever request
SCOPES: list[str] = ["https://www.googleapis.com/auth/gmail.readonly"]


def authenticate() -> Credentials:
    """Run or refresh the OAuth flow and persist the token.

    - If a valid token exists on disk, reuse it.
    - If the token is expired but has a refresh token, refresh it.
    - If refresh fails (e.g. 7-day expiry in Testing mode), re-run
      the browser-based consent flow.
    - If no token exists at all, run the consent flow from scratch.

    Returns the valid Credentials object.
    """
    ensure_data_dir()
    creds: Credentials | None = None

    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds)
            return creds
        except RefreshError:
            # Token refresh failed (e.g. 7-day expiry in Testing mode).
            # Fall through to the browser flow.
            pass

    # Fresh consent flow
    if not CREDENTIALS_PATH.exists():
        print(
            f"Error: OAuth client file not found at {CREDENTIALS_PATH}\n"
            "Download it from the Google Cloud Console and place it there.",
            file=sys.stderr,
        )
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(
        str(CREDENTIALS_PATH), SCOPES
    )
    creds = flow.run_local_server(port=0)
    _save_token(creds)
    return creds


def _save_token(creds: Credentials) -> None:
    """Write credentials to the token file."""
    TOKEN_PATH.write_text(creds.to_json())


def get_service(creds: Credentials) -> Resource:
    """Build and return a Gmail API service object."""
    return build("gmail", "v1", credentials=creds)


def get_profile(service: Resource) -> str:
    """Return the authenticated user's email address."""
    profile = service.users().getProfile(userId="me").execute()
    return profile["emailAddress"]
