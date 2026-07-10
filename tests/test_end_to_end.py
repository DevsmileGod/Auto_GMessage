"""End-to-end: the real send loop, the real SMTP client, a real SMTP server.

Nothing is mocked below the socket. These tests answer the question "will the
first message go out, and will the second one follow after the interval, to the
same recipient, before we move on?"
"""

import threading
import time
import tkinter as tk

import pytest
from smtp_server import Mailbox, SMTPTestServer

import ui
from exceptions import AuthenticationError
from gmail_client import Credentials, GmailClient
from sender import EmailSender, Message

ALICE = "alice@example.com"
BOB = "bob@example.com"

MESSAGES = [
    Message(subject="Intro", body="Hello, this is the first message."),
    Message(subject="Follow up", body="Hello again, this is the second message."),
]


@pytest.fixture
def mailbox():
    return Mailbox()


@pytest.fixture
def server(mailbox):
    with SMTPTestServer(mailbox) as running:
        yield running


@pytest.fixture
def client(server, mailbox):
    credentials = Credentials(email=mailbox.username, app_password=mailbox.password)
    client = GmailClient(credentials, host=server.host, port=server.port, use_starttls=False)
    client.verify_login()
    yield client
    client.close()


def run_send(email_sender, emails, interval=0, timeout=30):
    """Run a full send and block until it completes."""
    email_sender.interval_seconds = interval
    done = threading.Event()
    completion = {}

    def on_complete(stopped, results, retryable):
        completion.update(stopped=stopped, results=results, retryable=retryable)
        done.set()

    assert email_sender.start(emails=emails, messages=MESSAGES, on_complete=on_complete)
    assert done.wait(timeout=timeout), "send never completed"
    email_sender._thread.join(timeout=5)
    return completion


# ---------------------------------------------------------------- happy path


def test_one_recipient_receives_both_messages_in_order(client, mailbox):
    email_sender = EmailSender(client)
    completion = run_send(email_sender, [ALICE])

    assert len(mailbox.emails) == 2
    first, second = mailbox.emails

    assert first.to == ALICE and second.to == ALICE
    assert first.subject == "Intro"
    assert second.subject == "Follow up"
    assert first.body == "Hello, this is the first message."
    assert second.body == "Hello again, this is the second message."
    assert completion["stopped"] is False
    assert completion["retryable"] == []


def test_envelope_sender_and_recipient_are_correct(client, mailbox):
    EmailSender(client)
    run_send(EmailSender(client), [ALICE])

    for received in mailbox.emails:
        assert received.mail_from == mailbox.username
        assert received.rcpt_to == [ALICE]
        assert received.parsed["From"] == mailbox.username


def test_second_message_arrives_only_after_the_interval(client, mailbox):
    email_sender = EmailSender(client)
    run_send(email_sender, [ALICE], interval=2)

    assert len(mailbox.emails) == 2
    gap = mailbox.emails[1].received_at - mailbox.emails[0].received_at
    assert gap >= 2.0, f"follow-up arrived after only {gap:.2f}s"


def test_recipients_are_processed_one_at_a_time(client, mailbox):
    """Both of Alice's messages must land before Bob hears anything."""
    email_sender = EmailSender(client)
    run_send(email_sender, [ALICE, BOB], interval=1)

    order = [(e.to, e.subject) for e in mailbox.emails]
    assert order == [
        (ALICE, "Intro"),
        (ALICE, "Follow up"),
        (BOB, "Intro"),
        (BOB, "Follow up"),
    ]


def test_interval_separates_every_message_including_across_recipients(client, mailbox):
    email_sender = EmailSender(client)
    run_send(email_sender, [ALICE, BOB], interval=1)

    times = [e.received_at for e in mailbox.emails]
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert all(gap >= 1.0 for gap in gaps), gaps


def test_each_message_gets_a_unique_message_id(client, mailbox):
    run_send(EmailSender(client), [ALICE, BOB])

    ids = [e.parsed["Message-ID"] for e in mailbox.emails]
    assert all(ids), "every message needs a Message-ID"
    assert len(set(ids)) == len(ids), "Message-IDs must be unique"
    assert all(i.endswith("@gmail.com>") for i in ids), ids


def test_unicode_subject_and_body_survive_the_round_trip(client, mailbox):
    email_sender = EmailSender(client)
    messages = [
        Message(subject="Grüße 👋", body="Здравствуйте — first"),
        Message(subject="日本語", body="こんにちは — second"),
    ]
    done = threading.Event()
    email_sender.interval_seconds = 0
    email_sender.start(emails=[ALICE], messages=messages, on_complete=lambda *_: done.set())
    assert done.wait(timeout=15)
    email_sender._thread.join(timeout=5)

    assert mailbox.emails[0].subject == "Grüße 👋"
    assert mailbox.emails[1].subject == "日本語"
    assert mailbox.emails[0].body == "Здравствуйте — first"
    assert mailbox.emails[1].body == "こんにちは — second"


# ------------------------------------------------------------------ failures


def test_rejected_recipient_never_receives_the_follow_up(client, mailbox):
    mailbox.reject_recipients.add(ALICE)
    email_sender = EmailSender(client)

    completion = run_send(email_sender, [ALICE, BOB])

    delivered = [(e.to, e.subject) for e in mailbox.emails]
    assert delivered == [(BOB, "Intro"), (BOB, "Follow up")]
    assert completion["retryable"] == [ALICE]
    assert email_sender.pending_messages(ALICE) == [1, 2]


def test_retry_delivers_only_the_missing_message_no_duplicates(client, mailbox):
    """Message 1 lands, message 2 is refused, retry must not resend message 1."""
    email_sender = EmailSender(client)

    # Fail only the second message by rejecting the recipient partway through.
    original_send = client.send_email
    calls = {"n": 0}

    def send_email(to, subject, body, message_index=1):
        calls["n"] += 1
        if calls["n"] == 2:
            mailbox.reject_recipients.add(to)
        try:
            return original_send(to, subject, body, message_index)
        finally:
            mailbox.reject_recipients.discard(to)

    client.send_email = send_email
    completion = run_send(email_sender, [ALICE])

    assert [e.subject for e in mailbox.emails] == ["Intro"]
    assert completion["retryable"] == [ALICE]
    assert email_sender.pending_messages(ALICE) == [2]

    client.send_email = original_send
    done = threading.Event()
    email_sender.interval_seconds = 0
    email_sender.start_retry(
        emails=[ALICE], messages=MESSAGES, on_complete=lambda *_: done.set()
    )
    assert done.wait(timeout=15)
    email_sender._thread.join(timeout=5)

    subjects = [e.subject for e in mailbox.emails]
    assert subjects == ["Intro", "Follow up"], "retry must not deliver 'Intro' twice"
    assert email_sender.pending_messages(ALICE) == []


def test_wrong_app_password_is_rejected_at_sign_in(server, mailbox):
    credentials = Credentials(email=mailbox.username, app_password="wrongpassword123")
    client = GmailClient(credentials, host=server.host, port=server.port, use_starttls=False)

    with pytest.raises(AuthenticationError, match="App Password"):
        client.verify_login()


def test_server_restart_mid_run_does_not_lose_the_follow_up(client, mailbox):
    """Gmail hangs up on idle connections; the client must silently reconnect."""
    email_sender = EmailSender(client)
    email_sender.interval_seconds = 0

    assert client.send_email(ALICE, "Intro", "one", 1).success
    client._smtp.close()  # simulate the server dropping the idle socket
    assert client.send_email(ALICE, "Follow up", "two", 2).success

    assert [e.subject for e in mailbox.emails] == ["Intro", "Follow up"]


def test_stop_mid_run_leaves_the_second_message_unsent(client, mailbox):
    email_sender = EmailSender(client)
    email_sender.interval_seconds = 5
    done = threading.Event()

    email_sender.start(
        emails=[ALICE],
        messages=MESSAGES,
        on_result=lambda *_: email_sender.stop(),
        on_complete=lambda *_: done.set(),
    )
    assert done.wait(timeout=15)
    email_sender._thread.join(timeout=5)

    assert [e.subject for e in mailbox.emails] == ["Intro"]


# ----------------------------------------------------- the whole app, for real


def test_full_app_flow_paste_import_send_two_messages(
    tk_root, tmp_path, monkeypatch, server, mailbox, client
):
    """Drive the actual GUI: paste addresses, import, click Send.

    Covers the reported bug (paste box locking up) and the requested behaviour
    (two messages per recipient, one recipient at a time) in one pass.
    """
    monkeypatch.setattr(ui, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(ui, "TEMPLATES_PATH", tmp_path / "templates.json")
    monkeypatch.setattr(ui, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(ui, "load_credentials", lambda: None)
    monkeypatch.setattr(ui.messagebox, "showinfo", lambda *a, **k: None)
    monkeypatch.setattr(ui.messagebox, "showwarning", lambda *a, **k: None)
    monkeypatch.setattr(ui.messagebox, "showerror", lambda *a, **k: None)
    monkeypatch.setattr(ui.messagebox, "askyesno", lambda *a, **k: True)  # confirm the send

    window = tk.Toplevel(tk_root)
    window.withdraw()
    app = ui.GmailAutoSenderApp(root=window)
    try:
        # Sign in with the client pointed at our local server.
        app._client = client
        app._email_sender = EmailSender(client)
        app.config["interval_seconds"] = 1

        # Type two addresses and press Import.
        app.paste_text.insert("1.0", f"{ALICE}\n{BOB}\n")
        app._import_recipients_from_paste()
        assert app._get_recipient_emails() == [ALICE, BOB]
        assert str(app.paste_text.cget("state")) == tk.NORMAL

        app._editors[0].set_message("Intro", "Hello, this is the first message.")
        app._editors[1].set_message("Follow up", "Hello again, this is the second message.")

        app.start()
        assert app._email_sender.is_running
        assert str(app.paste_text.cget("state")) == tk.DISABLED  # locked during the send

        deadline = time.monotonic() + 60
        while app._email_sender.is_running and time.monotonic() < deadline:
            window.update()
            time.sleep(0.05)
        assert not app._email_sender.is_running, "send did not finish"
        window.update()
        app._pump()  # drain whatever the worker queued at the very end

        # Four emails: both of Alice's, then both of Bob's.
        assert [(e.to, e.subject) for e in mailbox.emails] == [
            (ALICE, "Intro"),
            (ALICE, "Follow up"),
            (BOB, "Intro"),
            (BOB, "Follow up"),
        ]
        times = [e.received_at for e in mailbox.emails]
        assert all(b - a >= 1.0 for a, b in zip(times, times[1:]))

        # Every recipient shows as fully sent, and the paste box works again.
        for email in (ALICE, BOB):
            assert app._recipient_records[email]["status"] == ui.STATUS_SENT
            assert app._recipient_records[email]["sent"] is True
        assert str(app.paste_text.cget("state")) == tk.NORMAL

        app.paste_text.insert("1.0", "carol@example.com")
        assert app.paste_text.get("1.0", tk.END).strip() == "carol@example.com"
    finally:
        app._client = None  # the client fixture owns closing it
        app._closing = True
        window.destroy()
