"""Tests for Google OAuth sign-in, XOAUTH2 auth, and encrypted credential storage."""

import json
import time

import pytest

import gmail_client
import google_auth
import secret_store
from exceptions import AuthenticationError, ConfigurationError
from gmail_client import Credentials, GmailClient, load_credentials, save_credentials
from google_auth import ClientConfig, OAuthToken

from smtp_server import Mailbox, SMTPTestServer

CONFIG = ClientConfig(client_id="cid.apps.googleusercontent.com", client_secret="csecret")


@pytest.fixture(autouse=True)
def temp_paths(tmp_path, monkeypatch):
    """Keep the credential and client-secret files out of the real project folder."""
    monkeypatch.setattr(gmail_client, "CREDENTIALS_PATH", tmp_path / "gmail_credentials.json")
    monkeypatch.setattr(google_auth, "CLIENT_PATH", tmp_path / "client_secret.json")
    return tmp_path


# ---------------------------------------------------------------- secret_store


def test_protected_secret_round_trips():
    assert secret_store.unprotect(secret_store.protect("hunter2")) == "hunter2"


def test_protected_secret_is_not_stored_in_the_clear():
    protected = secret_store.protect("hunter2")
    # Off Windows there is no DPAPI and the value passes through, which is the
    # documented fallback — only assert the encryption when it is actually available.
    if protected.startswith(secret_store.DPAPI_PREFIX):
        assert "hunter2" not in protected


def test_a_plaintext_secret_written_before_encryption_still_loads():
    assert secret_store.unprotect("abcdefghijklmnop") == "abcdefghijklmnop"


def test_empty_secret_round_trips_as_empty():
    assert secret_store.protect("") == ""
    assert secret_store.unprotect("") == ""


# ------------------------------------------------------------------- token


def test_a_token_with_no_access_token_is_expired():
    assert OAuthToken(refresh_token="r").is_expired()


def test_a_token_expiring_within_the_margin_is_already_expired():
    # Not yet expired by the clock, but too close to risk starting a send with it.
    token = OAuthToken(refresh_token="r", access_token="a", expires_at=time.time() + 30)
    assert token.is_expired()


def test_a_fresh_token_is_not_expired():
    token = OAuthToken(refresh_token="r", access_token="a", expires_at=time.time() + 3600)
    assert not token.is_expired()


def test_xoauth2_string_has_gmails_sasl_layout():
    assert (
        google_auth.xoauth2_string("me@gmail.com", "tok")
        == "user=me@gmail.com\x01auth=Bearer tok\x01\x01"
    )


# ------------------------------------------------------------- client config


def test_client_config_reads_the_installed_section(temp_paths):
    (temp_paths / "client_secret.json").write_text(
        json.dumps({"installed": {"client_id": "cid", "client_secret": "sec"}})
    )
    config = google_auth.load_client_config()
    assert (config.client_id, config.client_secret) == ("cid", "sec")


def test_a_missing_client_config_explains_the_setup(temp_paths):
    with pytest.raises(ConfigurationError, match="console.cloud.google.com"):
        google_auth.load_client_config()


def test_a_client_config_without_an_id_is_rejected(temp_paths):
    (temp_paths / "client_secret.json").write_text(json.dumps({"installed": {"client_id": ""}}))
    with pytest.raises(ConfigurationError, match="client_id"):
        google_auth.load_client_config()


# ------------------------------------------------------------- credentials


def test_credentials_refresh_the_access_token_when_it_has_expired(monkeypatch):
    monkeypatch.setattr(google_auth, "load_client_config", lambda: CONFIG)

    def fake_refresh(config, token):
        assert config is CONFIG
        token.access_token = "fresh"
        token.expires_at = time.time() + 3600
        return token

    monkeypatch.setattr(google_auth, "refresh_access_token", fake_refresh)

    credentials = Credentials(email="me@gmail.com", oauth=OAuthToken(refresh_token="r"))
    assert credentials.access_token() == "fresh"


def test_credentials_reuse_an_access_token_that_is_still_good(monkeypatch):
    def explode(*_args):
        raise AssertionError("must not refresh a token that has not expired")

    monkeypatch.setattr(google_auth, "refresh_access_token", explode)

    token = OAuthToken(refresh_token="r", access_token="good", expires_at=time.time() + 3600)
    assert Credentials(email="me@gmail.com", oauth=token).access_token() == "good"


def test_app_password_credentials_have_no_access_token():
    credentials = Credentials(email="me@gmail.com", app_password="abcdefghijklmnop")
    assert not credentials.uses_oauth
    with pytest.raises(ConfigurationError):
        credentials.access_token()


def test_oauth_credentials_need_no_app_password():
    Credentials(email="me@gmail.com", oauth=OAuthToken(refresh_token="r")).validate()


# -------------------------------------------------------------- persistence


def test_an_oauth_session_survives_a_restart(temp_paths):
    save_credentials(Credentials(email="me@gmail.com", oauth=OAuthToken(refresh_token="r3fresh")))

    restored = load_credentials()
    assert restored is not None
    assert restored.uses_oauth
    assert restored.email == "me@gmail.com"
    assert restored.oauth.refresh_token == "r3fresh"
    # The access token is short-lived and deliberately never written to disk.
    assert restored.oauth.access_token == ""


def test_the_saved_refresh_token_is_encrypted_at_rest(temp_paths):
    save_credentials(Credentials(email="me@gmail.com", oauth=OAuthToken(refresh_token="r3fresh")))

    raw = (temp_paths / "gmail_credentials.json").read_text()
    if secret_store.protect("probe").startswith(secret_store.DPAPI_PREFIX):
        assert "r3fresh" not in raw


def test_the_saved_app_password_is_encrypted_at_rest(temp_paths):
    save_credentials(Credentials(email="me@gmail.com", app_password="abcdefghijklmnop"))

    raw = (temp_paths / "gmail_credentials.json").read_text()
    if secret_store.protect("probe").startswith(secret_store.DPAPI_PREFIX):
        assert "abcdefghijklmnop" not in raw
    assert load_credentials().app_password == "abcdefghijklmnop"


def test_oauth_wins_over_a_stale_app_password_in_the_same_file(temp_paths):
    (temp_paths / "gmail_credentials.json").write_text(
        json.dumps(
            {
                "email": "me@gmail.com",
                "app_password": "abcdefghijklmnop",
                "oauth": {"refresh_token": "r3fresh"},
            }
        )
    )
    assert load_credentials().uses_oauth


# --------------------------------------------------------------- SMTP login


def test_smtp_authenticates_with_xoauth2_and_sends(monkeypatch):
    monkeypatch.setattr(google_auth, "load_client_config", lambda: CONFIG)

    mailbox = Mailbox(username="me@gmail.com", access_token="ya29.live")
    with SMTPTestServer(mailbox) as server:
        token = OAuthToken(
            refresh_token="r", access_token="ya29.live", expires_at=time.time() + 3600
        )
        client = GmailClient(
            Credentials(email="me@gmail.com", oauth=token),
            host=server.host,
            port=server.port,
            use_starttls=False,
        )
        result = client.send_email("them@example.com", "Hello", "Body")
        client.close()

    assert result.success, result.error
    assert mailbox.emails[0].to == "them@example.com"


def test_smtp_refreshes_an_expired_token_before_sending(monkeypatch):
    monkeypatch.setattr(google_auth, "load_client_config", lambda: CONFIG)

    def fake_refresh(_config, token):
        token.access_token = "ya29.live"
        token.expires_at = time.time() + 3600
        return token

    monkeypatch.setattr(google_auth, "refresh_access_token", fake_refresh)

    mailbox = Mailbox(username="me@gmail.com", access_token="ya29.live")
    with SMTPTestServer(mailbox) as server:
        # Starts with a stale token — the client must renew it, unprompted, to get in.
        stale = OAuthToken(refresh_token="r", access_token="ya29.dead", expires_at=time.time() - 1)
        client = GmailClient(
            Credentials(email="me@gmail.com", oauth=stale),
            host=server.host,
            port=server.port,
            use_starttls=False,
        )
        result = client.send_email("them@example.com", "Hello", "Body")
        client.close()

    assert result.success, result.error


def test_a_rejected_token_explains_how_to_recover(monkeypatch):
    monkeypatch.setattr(google_auth, "load_client_config", lambda: CONFIG)

    mailbox = Mailbox(username="me@gmail.com", reject_auth=True)
    with SMTPTestServer(mailbox) as server:
        token = OAuthToken(refresh_token="r", access_token="nope", expires_at=time.time() + 3600)
        client = GmailClient(
            Credentials(email="me@gmail.com", oauth=token),
            host=server.host,
            port=server.port,
            use_starttls=False,
        )
        with pytest.raises(AuthenticationError, match="Sign in with Google"):
            client.verify_login()
