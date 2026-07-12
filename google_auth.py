"""Google OAuth 2.0 sign-in for desktop apps — the "Sign in with Google" button.

The user clicks once, their browser opens Google's consent page, and we capture the
result on a loopback HTTP server. Google hands back a refresh token that we store and
silently exchange for a fresh access token whenever the old one expires, so the user
never signs in again.

The access token authenticates SMTP and IMAP via the XOAUTH2 mechanism, which both
Gmail endpoints accept in place of a password.

This is Google's "installed application" flow. The client secret it uses is NOT a
secret — Google says so explicitly for this client type, because anyone can read it
out of a shipped binary. PKCE is what actually protects the exchange: the auth code is
useless to anyone who does not also hold the verifier we generated in memory.

Stdlib only, so the app keeps its no-dependency build.
"""

import base64
import hashlib
import http.server
import json
import logging
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from typing import Optional

import paths
from exceptions import AuthenticationError, ConfigurationError

logger = logging.getLogger(__name__)

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v2/userinfo"

# mail.google.com is the one scope Gmail's SMTP and IMAP servers accept for XOAUTH2 —
# the narrower gmail.send / gmail.readonly scopes only work against the REST API.
SCOPES = "https://mail.google.com/ openid email"

# How early to renew an access token, so a send never starts with one about to expire.
EXPIRY_MARGIN_SECONDS = 120

CLIENT_PATH = paths.OAUTH_CLIENT_PATH

SETUP_HELP = (
    "Google sign-in needs a one-time setup, because Google requires every app to "
    "identify itself with its own OAuth client.\n\n"
    "1. Go to console.cloud.google.com and create a project.\n"
    "2. APIs & Services → OAuth consent screen → External → add your Gmail address "
    "under 'Test users'.\n"
    "3. APIs & Services → Credentials → Create credentials → OAuth client ID → "
    "Application type: Desktop app.\n"
    "4. Download the JSON and save it next to the app as:\n"
    f"   {CLIENT_PATH}\n\n"
    "Then click 'Sign in with Google' again. You can keep using an App Password "
    "in the meantime — the other tab still works."
)


@dataclass(frozen=True)
class ClientConfig:
    """The OAuth client identity, from the JSON downloaded from Google Cloud."""

    client_id: str
    client_secret: str


def client_config_exists() -> bool:
    return CLIENT_PATH.exists()


def load_client_config() -> ClientConfig:
    """Read client_secret.json as downloaded from the Cloud Console. Raises if unusable."""
    if not CLIENT_PATH.exists():
        raise ConfigurationError(SETUP_HELP)
    try:
        with CLIENT_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"Could not read {CLIENT_PATH}: {exc}") from exc

    # Google nests the fields under "installed" (desktop) or "web"; accept either,
    # and also a flat file in case someone hand-writes one.
    section = data.get("installed") or data.get("web") or data
    client_id = (section.get("client_id") or "").strip()
    client_secret = (section.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        raise ConfigurationError(
            f"{CLIENT_PATH} has no client_id/client_secret. Re-download it from the "
            "Google Cloud Console (Credentials → OAuth client ID → Desktop app)."
        )
    return ClientConfig(client_id=client_id, client_secret=client_secret)


@dataclass
class OAuthToken:
    """A Google refresh token plus the short-lived access token minted from it.

    Only the refresh token is worth persisting; the access token is cached purely to
    avoid a network round-trip before every single send.
    """

    refresh_token: str
    access_token: str = ""
    expires_at: float = 0.0

    def is_expired(self, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.time()
        return not self.access_token or now >= (self.expires_at - EXPIRY_MARGIN_SECONDS)

    def to_dict(self) -> dict:
        # The access token is deliberately not saved: it is dead within the hour, and
        # not writing it to disk means one less live secret sitting in a file.
        return {"refresh_token": self.refresh_token}

    @classmethod
    def from_dict(cls, data: dict) -> Optional["OAuthToken"]:
        refresh_token = (data.get("refresh_token") or "").strip()
        return cls(refresh_token=refresh_token) if refresh_token else None


def _post_token(payload: dict) -> dict:
    """POST to Google's token endpoint and return the parsed JSON."""
    body = urllib.parse.urlencode(payload).encode()
    request = urllib.request.Request(
        TOKEN_ENDPOINT,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            error = json.loads(exc.read().decode("utf-8", "replace"))
            detail = error.get("error_description") or error.get("error") or ""
        except (OSError, ValueError):
            # The error body is best-effort context; a bad one must not mask the failure.
            pass
        raise AuthenticationError(
            f"Google rejected the sign-in{': ' + detail if detail else ''}."
        ) from exc
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise AuthenticationError(f"Could not reach Google: {exc}") from exc


def refresh_access_token(config: ClientConfig, token: OAuthToken) -> OAuthToken:
    """Exchange the refresh token for a new access token, in place. Raises on failure."""
    data = _post_token(
        {
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "refresh_token": token.refresh_token,
            "grant_type": "refresh_token",
        }
    )
    access_token = data.get("access_token")
    if not access_token:
        raise AuthenticationError(
            "Google did not return an access token. Sign in with Google again."
        )
    token.access_token = access_token
    token.expires_at = time.time() + float(data.get("expires_in", 3600))
    logger.info("Refreshed Google access token")
    return token


def fetch_email(access_token: str) -> str:
    """Ask Google which account just signed in."""
    request = urllib.request.Request(
        USERINFO_ENDPOINT, headers={"Authorization": f"Bearer {access_token}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.load(response)
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise AuthenticationError(f"Could not read the Google account address: {exc}") from exc
    email = (data.get("email") or "").strip()
    if not email:
        raise AuthenticationError("Google did not return an email address for this account.")
    return email


def xoauth2_string(email: str, access_token: str) -> str:
    """The SASL XOAUTH2 initial response Gmail's SMTP and IMAP servers expect."""
    return f"user={email}\x01auth=Bearer {access_token}\x01\x01"


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Catches Google's redirect back to localhost and reads the ?code= off it."""

    result: dict = {}

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        _CallbackHandler.result = {k: v[0] for k, v in params.items()}

        ok = "code" in _CallbackHandler.result
        message = (
            "Signed in. You can close this tab and go back to the app."
            if ok
            else "Sign-in was cancelled. You can close this tab."
        )
        page = (
            "<!doctype html><meta charset='utf-8'>"
            "<title>Gmail Auto Sender</title>"
            "<body style='font-family:system-ui;text-align:center;padding-top:15vh'>"
            f"<h2>{message}</h2></body>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def log_message(self, *args) -> None:
        """Silence the default stderr access log."""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def sign_in(config: ClientConfig, timeout: float = 300.0) -> tuple[str, OAuthToken]:
    """Run the full browser consent flow. Returns (email, token). Raises on failure.

    Blocks until the user finishes in the browser, so call it off the UI thread.
    """
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    state = secrets.token_urlsafe(24)

    port = _free_port()
    redirect_uri = f"http://127.0.0.1:{port}"

    params = {
        "client_id": config.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        # offline + consent is what makes Google return a refresh token. Without
        # prompt=consent it withholds one on every sign-in after the first, and the
        # app would silently lose the ability to reconnect on its own.
        "access_type": "offline",
        "prompt": "consent",
    }
    auth_url = f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"

    _CallbackHandler.result = {}
    server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.timeout = 1.0

    done = threading.Event()

    def serve() -> None:
        deadline = time.time() + timeout
        while not done.is_set() and time.time() < deadline:
            server.handle_request()
            if _CallbackHandler.result:
                break
        done.set()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    if not webbrowser.open(auth_url):
        done.set()
        server.server_close()
        raise AuthenticationError(
            "Could not open a browser for Google sign-in. Open this URL manually:\n\n" + auth_url
        )

    done.wait(timeout)
    server.server_close()
    result = _CallbackHandler.result
    _CallbackHandler.result = {}

    if not result:
        raise AuthenticationError("Google sign-in timed out. Please try again.")
    if result.get("error"):
        raise AuthenticationError(f"Google sign-in was refused: {result['error']}")
    if result.get("state") != state:
        # A mismatch means the response did not come from the request we started.
        raise AuthenticationError("Google sign-in failed a security check. Please try again.")
    code = result.get("code")
    if not code:
        raise AuthenticationError("Google sign-in was cancelled.")

    data = _post_token(
        {
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
    )
    refresh_token = data.get("refresh_token")
    access_token = data.get("access_token")
    if not refresh_token or not access_token:
        raise AuthenticationError(
            "Google did not return a refresh token. Remove this app at "
            "myaccount.google.com/permissions and sign in again."
        )

    token = OAuthToken(
        refresh_token=refresh_token,
        access_token=access_token,
        expires_at=time.time() + float(data.get("expires_in", 3600)),
    )
    email = fetch_email(access_token)
    logger.info("Google sign-in complete for %s", email)
    return email, token
