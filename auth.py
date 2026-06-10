"""Gmail OAuth2 authentication."""

import os
from pathlib import Path

from dotenv import load_dotenv
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
TOKEN_PATH = Path("token.json")
CREDENTIALS_PATH = Path(os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json"))
OAUTH_TIMEOUT_SECONDS = 180


def is_logged_in() -> bool:
    """Return True if a valid token.json exists (or can be refreshed)."""
    if not TOKEN_PATH.exists():
        return False
    try:
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        if creds.valid:
            return True
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json())
            return True
    except (OSError, RefreshError, ValueError):
        return False
    return False


def get_credentials() -> Credentials:
    """Load, refresh, or obtain OAuth2 credentials for Gmail API."""
    creds = None

    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as exc:
                TOKEN_PATH.unlink(missing_ok=True)
                raise RuntimeError(
                    "Gmail login expired. Please sign in again."
                ) from exc
        else:
            creds = _run_oauth_flow()

        TOKEN_PATH.write_text(creds.to_json())

    return creds


def _run_oauth_flow() -> Credentials:
    """Open browser OAuth flow. Must run on the main thread (not a worker thread)."""
    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(
            f"OAuth credentials file not found: {CREDENTIALS_PATH.resolve()}\n"
            "Download Desktop OAuth JSON from Google Cloud Console and save as credentials.json."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)

    try:
        return flow.run_local_server(
            port=0,
            open_browser=True,
            timeout_seconds=OAUTH_TIMEOUT_SECONDS,
            authorization_prompt_message=(
                "Opening your browser for Gmail sign-in.\n"
                "If it does not open, copy the URL from the terminal into your browser."
            ),
            success_message=(
                "Gmail sign-in successful. Return to the app and click Send again."
            ),
        )
    except Exception as exc:
        error_name = type(exc).__name__
        if "Timeout" in error_name or "timeout" in str(exc).lower():
            raise RuntimeError(
                "Gmail sign-in timed out.\n\n"
                "1. Click the URL printed in the terminal (or PowerShell window)\n"
                "2. Sign in with your Google account\n"
                "3. Click Allow / Continue\n"
                "4. Wait until the browser shows success, then try Send again\n\n"
                "Also check: your Gmail is added as a Test user on the Google OAuth consent screen."
            ) from exc
        raise RuntimeError(f"Gmail sign-in failed: {exc}") from exc


def sign_out() -> None:
    """Remove saved login so the next sign-in can use a different Gmail account."""
    TOKEN_PATH.unlink(missing_ok=True)


def get_gmail_service() -> Resource:
    """Return an authenticated Gmail API service object."""
    creds = get_credentials()
    return build("gmail", "v1", credentials=creds)
