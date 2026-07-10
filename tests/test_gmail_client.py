"""Tests for the Gmail SMTP client."""

import json
import smtplib

import pytest

import gmail_client
from exceptions import AuthenticationError, ConfigurationError
from gmail_client import (
    Credentials,
    GmailClient,
    build_client,
    build_references,
    clear_credentials,
    load_credentials,
    make_reply_subject,
    normalize_app_password,
    save_credentials,
)

CREDS = Credentials(email="sender@gmail.com", app_password="abcdefghijklmnop")


class FakeSMTP:
    """Stands in for smtplib.SMTP. Records the conversation."""

    instances: list["FakeSMTP"] = []
    auth_error = False
    connect_error = False

    def __init__(self, host, port, local_hostname=None, timeout=None):
        if FakeSMTP.connect_error:
            raise OSError("network unreachable")
        self.host = host
        self.port = port
        self.local_hostname = local_hostname
        self.sent = []
        self.logged_in_as = None
        self.starttls_called = False
        self.quit_called = False
        self.noop_status = 250
        FakeSMTP.instances.append(self)

    def ehlo(self):
        return 250, b"ok"

    def starttls(self, context=None):
        self.starttls_called = True

    def login(self, user, password):
        if FakeSMTP.auth_error:
            raise smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")
        self.logged_in_as = (user, password)

    def noop(self):
        if self.noop_status != 250:
            raise smtplib.SMTPServerDisconnected("connection closed")
        return self.noop_status, b"ok"

    def send_message(self, message):
        self.sent.append(message)

    def quit(self):
        self.quit_called = True

    def close(self):
        pass


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.auth_error = False
    FakeSMTP.connect_error = False
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


@pytest.fixture(autouse=True)
def temp_credentials_path(tmp_path, monkeypatch):
    monkeypatch.setattr(gmail_client, "CREDENTIALS_PATH", tmp_path / "gmail_credentials.json")
    return gmail_client.CREDENTIALS_PATH


# ------------------------------------------------------------- credentials


def test_app_password_spaces_are_stripped():
    assert normalize_app_password("abcd efgh ijkl mnop") == "abcdefghijklmnop"


def test_credentials_round_trip(temp_credentials_path):
    save_credentials(CREDS)
    assert json.loads(temp_credentials_path.read_text())["email"] == CREDS.email
    assert load_credentials() == CREDS


def test_load_credentials_returns_none_when_absent():
    assert load_credentials() is None


def test_load_credentials_returns_none_on_corrupt_file(temp_credentials_path):
    temp_credentials_path.write_text("{not json")
    assert load_credentials() is None


def test_load_credentials_normalizes_a_spaced_password(temp_credentials_path):
    temp_credentials_path.write_text(
        json.dumps({"email": CREDS.email, "app_password": "abcd efgh ijkl mnop"})
    )
    assert load_credentials().app_password == "abcdefghijklmnop"


def test_clear_credentials_is_safe_when_nothing_is_saved():
    clear_credentials()
    clear_credentials()


def test_clear_credentials_removes_the_file(temp_credentials_path):
    save_credentials(CREDS)
    clear_credentials()
    assert not temp_credentials_path.exists()


@pytest.mark.parametrize("email", ["", "no-at-sign", "missing@domain"])
def test_invalid_email_is_rejected(email):
    with pytest.raises(ConfigurationError):
        GmailClient(Credentials(email=email, app_password="pw"))


def test_empty_password_is_rejected():
    with pytest.raises(ConfigurationError):
        GmailClient(Credentials(email=CREDS.email, app_password=""))


# ------------------------------------------------------------------- login


def test_login_uses_starttls_and_the_app_password(fake_smtp):
    client = GmailClient(CREDS)
    client.verify_login()

    smtp = fake_smtp.instances[-1]
    assert (smtp.host, smtp.port) == ("smtp.gmail.com", 587)
    assert smtp.starttls_called
    assert smtp.logged_in_as == (CREDS.email, CREDS.app_password)
    assert client.is_logged_in()


def test_rejected_password_raises_with_app_password_guidance(fake_smtp):
    fake_smtp.auth_error = True
    with pytest.raises(AuthenticationError, match="App Password"):
        GmailClient(CREDS).verify_login()


def test_unreachable_server_raises_authentication_error(fake_smtp):
    fake_smtp.connect_error = True
    with pytest.raises(AuthenticationError, match="Could not reach"):
        GmailClient(CREDS).verify_login()


def test_build_client_verifies_before_returning(fake_smtp):
    client = build_client(CREDS)
    assert client.is_logged_in()
    assert fake_smtp.instances[-1].logged_in_as is not None


def test_build_client_propagates_bad_credentials(fake_smtp):
    fake_smtp.auth_error = True
    with pytest.raises(AuthenticationError):
        build_client(CREDS)


def test_helo_name_avoids_a_reverse_dns_lookup(fake_smtp):
    """An unset local_hostname makes smtplib call socket.getfqdn() on every connect."""
    build_client(CREDS)
    assert fake_smtp.instances[-1].local_hostname == "gmail.com"


def test_close_quits_the_connection_but_keeps_the_session(fake_smtp):
    client = build_client(CREDS)
    client.close()

    assert fake_smtp.instances[-1].quit_called
    assert not client.has_connection
    # Closing a socket is not signing out; the next send just reconnects.
    assert client.is_logged_in()


# -------------------------------------------------------------------- send


def test_send_email_builds_a_correct_message(fake_smtp):
    client = build_client(CREDS)
    result = client.send_email("to@example.com", "Subject line", "Body text", message_index=2)

    assert result.success
    assert result.message_index == 2
    message = fake_smtp.instances[-1].sent[0]
    assert message["From"] == CREDS.email
    assert message["To"] == "to@example.com"
    assert message["Subject"] == "Subject line"
    assert message["Date"]
    assert result.message_id == message["Message-ID"]
    assert message.get_content().strip() == "Body text"


def test_send_email_rejects_an_invalid_recipient(fake_smtp):
    client = build_client(CREDS)
    result = client.send_email("not-an-email", "s", "b")

    assert not result.success
    assert "Invalid recipient" in result.error
    assert fake_smtp.instances[-1].sent == []


def test_refused_recipient_is_returned_not_raised(fake_smtp):
    client = build_client(CREDS)
    smtp = fake_smtp.instances[-1]

    def boom(message):
        raise smtplib.SMTPRecipientsRefused({"to@example.com": (550, b"No such user")})

    smtp.send_message = boom
    result = client.send_email("to@example.com", "s", "b")

    assert not result.success
    assert result.error


def test_one_refused_recipient_does_not_end_the_session(fake_smtp):
    """A 550 for one address must not tear down the connection or the login."""
    client = build_client(CREDS)
    smtp = fake_smtp.instances[-1]
    good_send = smtp.send_message

    def refuse_once(message):
        smtp.send_message = good_send
        raise smtplib.SMTPRecipientsRefused({"bad@example.com": (550, b"No such user")})

    smtp.send_message = refuse_once
    assert not client.send_email("bad@example.com", "s", "b").success

    assert client.is_logged_in(), "a refused recipient must not look like a sign-out"
    assert client.has_connection, "the connection was still healthy"

    assert client.send_email("good@example.com", "s", "b").success
    assert len(fake_smtp.instances) == 1, "no reconnect should have been needed"


def test_stale_connection_is_reestablished_before_the_next_send(fake_smtp):
    """Gmail drops idle connections; a long interval must not break the follow-up."""
    client = build_client(CREDS)
    first = fake_smtp.instances[-1]
    assert client.send_email("to@example.com", "s", "one").success

    first.noop_status = 421  # server hung up while we waited out the interval

    assert client.send_email("to@example.com", "s", "two").success
    second = fake_smtp.instances[-1]

    assert second is not first, "expected a fresh connection"
    assert second.logged_in_as == (CREDS.email, CREDS.app_password)
    assert len(second.sent) == 1


def test_healthy_connection_is_reused_across_sends(fake_smtp):
    client = build_client(CREDS)
    client.send_email("a@example.com", "s", "b")
    client.send_email("b@example.com", "s", "b")

    assert len(fake_smtp.instances) == 1
    assert len(fake_smtp.instances[0].sent) == 2


def test_send_after_a_dropped_socket_reconnects(fake_smtp):
    client = build_client(CREDS)
    smtp = fake_smtp.instances[-1]
    smtp.send_message = lambda m: (_ for _ in ()).throw(smtplib.SMTPServerDisconnected("gone"))

    failed = client.send_email("to@example.com", "s", "b")
    assert not failed.success
    assert not client.has_connection, "dropped socket should be discarded"
    assert client.is_logged_in(), "but the credentials are still good"

    assert client.send_email("to@example.com", "s", "b").success
    assert len(fake_smtp.instances) == 2


# --------------------------------------------------------------- reply send


def test_send_reply_sets_threading_headers(fake_smtp):
    client = build_client(CREDS)
    result = client.send_reply(
        to="to@example.com",
        body="Thanks!",
        in_reply_to="<their-reply@mail>",
        references="<orig@x> <their-reply@mail>",
        subject="Re: Hello",
    )

    assert result.success
    assert result.message_index == 2
    message = fake_smtp.instances[-1].sent[-1]
    assert message["To"] == "to@example.com"
    assert message["Subject"] == "Re: Hello"
    assert message["In-Reply-To"] == "<their-reply@mail>"
    assert message["References"] == "<orig@x> <their-reply@mail>"
    assert message.get_content().strip() == "Thanks!"


def test_send_reply_rejects_invalid_recipient(fake_smtp):
    client = build_client(CREDS)
    result = client.send_reply("bad", "b", "<x>", "<x>", "Re: x")
    assert not result.success
    assert "Invalid recipient" in result.error


def test_plain_send_email_has_no_threading_headers(fake_smtp):
    client = build_client(CREDS)
    client.send_email("to@example.com", "Subject", "Body")
    message = fake_smtp.instances[-1].sent[-1]
    assert message["In-Reply-To"] is None
    assert message["References"] is None


@pytest.mark.parametrize(
    "subject, expected",
    [
        ("Hello", "Re: Hello"),
        ("Re: Hello", "Re: Hello"),
        ("re: hello", "Re: hello"),
        ("  RE:  Hello ", "Re: Hello"),
        ("", "Re: "),
    ],
)
def test_make_reply_subject_never_stacks_re(subject, expected):
    assert make_reply_subject(subject) == expected


def test_build_references_appends_without_duplicating():
    assert build_references("<a@x> <b@x>", "<c@x>") == "<a@x> <b@x> <c@x>"
    assert build_references("<a@x> <c@x>", "<c@x>") == "<a@x> <c@x>"
    assert build_references("", "<c@x>") == "<c@x>"
