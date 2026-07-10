"""End-to-end: real Campaign + real SMTP client + real in-process SMTP server.

Replies come from a scripted fake inbox (a live IMAP server isn't practical in a
test); everything from the Campaign down to the SMTP socket is real.
"""

import threading
import time
import tkinter as tk

import pytest
from conftest import Collector, FakeInbox, run_campaign
from smtp_server import Mailbox, SMTPTestServer

import campaign_state
import message_store
import ui
from campaign_state import CampaignState
from gmail_client import Credentials, GmailClient
from message_store import MessageStore
from sender import Campaign, CampaignCallbacks

ALICE = "alice@example.com"
BOB = "bob@example.com"


@pytest.fixture
def mailbox():
    return Mailbox()


@pytest.fixture
def server(mailbox):
    with SMTPTestServer(mailbox) as running:
        yield running


@pytest.fixture
def client(server, mailbox):
    creds = Credentials(email=mailbox.username, app_password=mailbox.password)
    c = GmailClient(creds, host=server.host, port=server.port, use_starttls=False)
    c.verify_login()
    yield c
    c.close()


def build(tmp_path, emails):
    store = MessageStore(tmp_path / "messages.json")
    store.add_first("Intro A", "Hello, this is intro A.")
    store.add_first("Intro B", "Hello, this is intro B.")
    store.set_second("Thanks for getting back to me!")
    state = CampaignState(tmp_path / "state.json")
    state.add_emails(emails)
    state.begin()
    return store, state


# --------------------------------------------------------------- outreach


def test_distinct_first_messages_on_the_wire(client, tmp_path, mailbox):
    store, state = build(tmp_path, [ALICE, BOB])
    inbox = FakeInbox()
    inbox.add_reply(ALICE)
    inbox.add_reply(BOB)
    campaign = Campaign(client, store, inbox, state, interval_seconds=0, poll_interval_seconds=0)

    run_campaign(campaign, Collector())

    firsts = {e.to: e for e in mailbox.emails if not e.parsed["In-Reply-To"]}
    assert firsts[ALICE].subject == "Intro A"
    assert firsts[BOB].subject == "Intro B"
    assert firsts[ALICE].body == "Hello, this is intro A."


# --------------------------------------------------------------- follow-up


def test_reply_produces_threaded_body_only_follow_up(client, tmp_path, mailbox):
    store, state = build(tmp_path, [ALICE])
    inbox = FakeInbox()
    inbox.add_reply(ALICE, reply_message_id="<a-reply@mail>", references="<orig@x>", subject="Re: Intro A")
    campaign = Campaign(client, store, inbox, state, interval_seconds=0, poll_interval_seconds=0)

    run_campaign(campaign, Collector())

    followups = [e for e in mailbox.emails if e.parsed["In-Reply-To"]]
    assert len(followups) == 1
    reply = followups[0].parsed
    assert reply["To"] == ALICE
    assert reply["Subject"] == "Re: Intro A"
    assert reply["In-Reply-To"] == "<a-reply@mail>"
    assert "<a-reply@mail>" in reply["References"]
    assert followups[0].body == "Thanks for getting back to me!"


def test_resume_after_restart_contacts_remaining_and_answers_earlier_batch(client, tmp_path, mailbox):
    """Two messages, three recipients: batch, 'restart', unlock, resume."""
    store = MessageStore(tmp_path / "messages.json")
    store.add_first("M1", "Body 1")
    store.add_first("M2", "Body 2")
    store.set_second("Follow-up")
    state = CampaignState(tmp_path / "state.json")
    state.add_emails([ALICE, BOB, "carol@example.com"])
    state.begin()

    # First run: contact two, then park on the exhausted pool.
    c1 = Campaign(client, store, FakeInbox(), state, interval_seconds=0, poll_interval_seconds=0)
    done1 = threading.Event()
    c1.start(CampaignCallbacks(on_waiting=lambda *_: c1.stop(), on_complete=lambda s: done1.set()))
    assert done1.wait(timeout=10)
    c1._thread.join(timeout=5)
    assert state.cursor == 2
    first_batch = [e.to for e in mailbox.emails if not e.parsed["In-Reply-To"]]
    assert first_batch == [ALICE, BOB]

    # Restart: reload from disk, 24h elapsed (unlock), everyone replies.
    store.reset_cooldowns()
    store2 = MessageStore(tmp_path / "messages.json")
    state2 = CampaignState(tmp_path / "state.json")
    inbox2 = FakeInbox()
    for e in (ALICE, BOB, "carol@example.com"):
        inbox2.add_reply(e)
    c2 = Campaign(client, store2, inbox2, state2, interval_seconds=0, poll_interval_seconds=0)
    run_campaign(c2, Collector())

    all_firsts = [e.to for e in mailbox.emails if not e.parsed["In-Reply-To"]]
    replies = {e.to for e in mailbox.emails if e.parsed["In-Reply-To"]}
    assert all_firsts == [ALICE, BOB, "carol@example.com"]  # carol contacted after resume
    assert replies == {ALICE, BOB, "carol@example.com"}     # earlier batch answered too
    assert state2.is_finished()


# ------------------------------------------------------ the whole app, live


def test_full_app_campaign_import_start_and_autoreply(
    tk_root, tmp_path, monkeypatch, server, mailbox, client
):
    monkeypatch.setattr(ui, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(ui, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(message_store, "MESSAGES_PATH", tmp_path / "messages.json")
    monkeypatch.setattr(campaign_state, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(ui, "load_credentials", lambda: None)
    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(ui.messagebox, name, lambda *a, **k: None)
    monkeypatch.setattr(ui.messagebox, "askyesno", lambda *a, **k: True)

    inbox = FakeInbox()
    inbox.add_reply(ALICE, subject="Re: Intro A")
    inbox.add_reply(BOB, subject="Re: Intro B")

    window = tk.Toplevel(tk_root)
    window.withdraw()
    app = ui.GmailAutoSenderApp(root=window)
    try:
        app._client = client
        app._inbox = inbox
        app._store.add_first("Intro A", "Hello A")
        app._store.add_first("Intro B", "Hello B")
        app._store.set_second("Thanks for replying!")
        app._refresh_first_list()
        app.config["interval_seconds"] = 0
        app.config["poll_interval_seconds"] = 0

        app.paste_text.insert("1.0", f"{ALICE}\n{BOB}\n")
        app._import_recipients_from_paste()
        assert sorted(app._get_recipient_emails()) == [ALICE, BOB]

        app.start()
        assert app._campaign is not None
        assert str(app.paste_text.cget("state")) == tk.DISABLED

        deadline = time.monotonic() + 30
        while app._campaign.is_running and time.monotonic() < deadline:
            window.update()
            time.sleep(0.02)
        assert not app._campaign.is_running, "campaign did not finish"
        window.update()
        app._pump()

        firsts = [e for e in mailbox.emails if not e.parsed["In-Reply-To"]]
        replies = [e for e in mailbox.emails if e.parsed["In-Reply-To"]]
        assert {e.to for e in firsts} == {ALICE, BOB}
        assert {e.to for e in replies} == {ALICE, BOB}

        for email in (ALICE, BOB):
            assert app._state.get(email).status == campaign_state.STATUS_DONE
        assert str(app.paste_text.cget("state")) == tk.NORMAL
    finally:
        app._client = None
        app._inbox = None
        app._closing = True
        window.destroy()
