"""Tests for IMAP reply detection (matching logic, faked imaplib)."""

import imaplib
import time

import pytest

import imap_client
from exceptions import AuthenticationError
from gmail_client import Credentials
from imap_client import GmailInbox, SentInfo

CREDS = Credentials(email="sender@gmail.com", app_password="abcdefghijklmnop")
ALICE = "alice@example.com"
BOB = "bob@example.com"


def raw_headers(sender, message_id, subject, in_reply_to="", references=""):
    lines = [f"From: {sender}", f"Message-ID: {message_id}", f"Subject: {subject}"]
    if in_reply_to:
        lines.append(f"In-Reply-To: {in_reply_to}")
    if references:
        lines.append(f"References: {references}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")


class FakeIMAP:
    login_error = False

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.messages = list(FakeIMAP.inbox)  # snapshot
        self.logged_out = False

    def login(self, user, password):
        if FakeIMAP.login_error:
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")
        return "OK", [b"logged in"]

    def select(self, mailbox):
        return "OK", [b"1"]

    def noop(self):
        return "OK", [b""]

    def search(self, charset, *criteria):
        # criteria like: ("FROM", '"alice@example.com"', "SINCE", "01-Jan-2026")
        sender = None
        for i, token in enumerate(criteria):
            if token == "FROM" and i + 1 < len(criteria):
                sender = criteria[i + 1].strip('"').lower()
        nums = [
            str(idx + 1).encode()
            for idx, m in enumerate(self.messages)
            if sender is None or m["from"].lower() == sender
        ]
        return "OK", [b" ".join(nums)]

    def fetch(self, num, spec):
        idx = int(num) - 1
        if idx < 0 or idx >= len(self.messages):
            return "NO", [None]
        m = self.messages[idx]
        raw = raw_headers(
            m["from"], m["message_id"], m["subject"],
            m.get("in_reply_to", ""), m.get("references", ""),
        )
        return "OK", [(num + b" (BODY[HEADER])", raw)]

    def logout(self):
        self.logged_out = True
        return "OK", [b"bye"]


@pytest.fixture(autouse=True)
def fake_imap(monkeypatch):
    FakeIMAP.inbox = []
    FakeIMAP.login_error = False
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP)
    return FakeIMAP


def pending_for(email, message_id, subject="Hello", sent_at=None):
    return {
        email.lower(): SentInfo(
            email=email, message_id=message_id, subject=subject,
            sent_at=sent_at if sent_at is not None else time.time(),
        )
    }


# ----------------------------------------------------------------- matching


def test_matches_reply_by_in_reply_to_header(fake_imap):
    fake_imap.inbox = [
        {"from": ALICE, "message_id": "<r1@mail>", "subject": "Re: Hello", "in_reply_to": "<sent-1@x>"}
    ]
    inbox = GmailInbox(CREDS)
    replies = inbox.find_replies(pending_for(ALICE, "<sent-1@x>"))

    assert len(replies) == 1
    assert replies[0].email == ALICE
    assert replies[0].reply_message_id == "<r1@mail>"


def test_matches_reply_by_references_header(fake_imap):
    fake_imap.inbox = [
        {"from": ALICE, "message_id": "<r1@mail>", "subject": "Re: Hello",
         "references": "<sent-1@x> <other@x>"}
    ]
    inbox = GmailInbox(CREDS)
    replies = inbox.find_replies(pending_for(ALICE, "<sent-1@x>"))
    assert len(replies) == 1


def test_matches_by_subject_when_headers_absent(fake_imap):
    fake_imap.inbox = [{"from": ALICE, "message_id": "<r1@mail>", "subject": "RE: Hello"}]
    inbox = GmailInbox(CREDS)
    replies = inbox.find_replies(pending_for(ALICE, "<sent-1@x>", subject="Hello"))
    assert len(replies) == 1


def test_no_match_when_sender_differs(fake_imap):
    fake_imap.inbox = [
        {"from": BOB, "message_id": "<r@mail>", "subject": "Re: Hello", "in_reply_to": "<sent-1@x>"}
    ]
    inbox = GmailInbox(CREDS)
    assert inbox.find_replies(pending_for(ALICE, "<sent-1@x>")) == []


def test_no_match_when_neither_header_nor_subject_align(fake_imap):
    fake_imap.inbox = [{"from": ALICE, "message_id": "<r@mail>", "subject": "Unrelated thread"}]
    inbox = GmailInbox(CREDS)
    assert inbox.find_replies(pending_for(ALICE, "<sent-1@x>", subject="Hello")) == []


def test_only_replied_recipient_is_returned(fake_imap):
    fake_imap.inbox = [
        {"from": ALICE, "message_id": "<r@mail>", "subject": "Re: Hello", "in_reply_to": "<sent-A@x>"}
    ]
    inbox = GmailInbox(CREDS)
    pending = {}
    pending.update(pending_for(ALICE, "<sent-A@x>"))
    pending.update(pending_for(BOB, "<sent-B@x>"))
    replies = inbox.find_replies(pending)
    assert [r.email for r in replies] == [ALICE]


def test_returns_one_reply_per_recipient_even_with_multiple_matches(fake_imap):
    fake_imap.inbox = [
        {"from": ALICE, "message_id": "<r1@mail>", "subject": "Re: Hello", "in_reply_to": "<sent-1@x>"},
        {"from": ALICE, "message_id": "<r2@mail>", "subject": "Re: Hello", "in_reply_to": "<sent-1@x>"},
    ]
    inbox = GmailInbox(CREDS)
    replies = inbox.find_replies(pending_for(ALICE, "<sent-1@x>"))
    assert len(replies) == 1


# -------------------------------------------------------------------- login


def test_verify_login_raises_with_imap_guidance(fake_imap):
    fake_imap.login_error = True
    with pytest.raises(AuthenticationError, match="IMAP"):
        GmailInbox(CREDS).verify_login()


def test_build_inbox_verifies(fake_imap):
    inbox = imap_client.build_inbox(CREDS)
    assert inbox is not None
    inbox.close()


def test_close_logs_out(fake_imap):
    inbox = imap_client.build_inbox(CREDS)
    inbox.close()
    # a fresh connection is made on next use
    inbox.find_replies({})  # no pending, no error
