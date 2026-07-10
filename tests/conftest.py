"""Shared test fixtures."""

import sys
import threading
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gmail_client import SendResult  # noqa: E402
from imap_client import DetectedReply  # noqa: E402
from sender import CampaignCallbacks  # noqa: E402


@pytest.fixture(scope="session")
def tk_root():
    """One Tcl interpreter for the whole session.

    Creating and destroying Tk() roots per test is flaky on Windows, so tests
    that need a window get a Toplevel off this root instead.
    """
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - headless CI
        pytest.skip(f"no display available: {exc}")
    root.withdraw()
    yield root
    root.destroy()


@dataclass
class SentRecord:
    kind: str  # "first" or "reply"
    to: str
    subject: str
    body: str
    message_index: int
    in_reply_to: str = ""
    references: str = ""


class FakeGmailClient:
    """Records sends instead of touching the network."""

    def __init__(self, fail_first=None, fail_reply=None):
        self.records: list[SentRecord] = []
        self.fail_first = {e.lower() for e in (fail_first or [])}
        self.fail_reply = {e.lower() for e in (fail_reply or [])}
        self.email = "sender@gmail.com"
        self._n = 0
        self._lock = threading.Lock()

    def is_logged_in(self) -> bool:
        return True

    def close(self) -> None:
        pass

    def send_email(self, to, subject, body, message_index=1) -> SendResult:
        with self._lock:
            self._n += 1
            n = self._n
            self.records.append(SentRecord("first", to, subject, body, message_index))
        if to.lower() in self.fail_first:
            return SendResult(email=to, success=False, message_index=message_index, error="fail")
        return SendResult(email=to, success=True, message_index=message_index, message_id=f"<first-{n}@x>")

    def send_reply(self, to, body, in_reply_to, references, subject, message_index=2) -> SendResult:
        with self._lock:
            self._n += 1
            n = self._n
            self.records.append(
                SentRecord("reply", to, subject, body, message_index, in_reply_to, references)
            )
        if to.lower() in self.fail_reply:
            return SendResult(email=to, success=False, message_index=message_index, error="fail")
        return SendResult(email=to, success=True, message_index=message_index, message_id=f"<reply-{n}@x>")

    def firsts(self) -> list[SentRecord]:
        return [r for r in self.records if r.kind == "first"]

    def replies(self) -> list[SentRecord]:
        return [r for r in self.records if r.kind == "reply"]


class FakeInbox:
    """Returns scripted replies for pending recipients."""

    def __init__(self):
        self._scripted: dict[str, DetectedReply] = {}
        self.calls = 0
        self.closed = False

    def add_reply(self, email, reply_message_id="<their-reply@x>", references="", subject="Re: Hello"):
        self._scripted[email.lower()] = DetectedReply(
            email=email, reply_message_id=reply_message_id,
            reply_references=references, reply_subject=subject,
        )

    def find_replies(self, pending) -> list[DetectedReply]:
        self.calls += 1
        return [self._scripted[key] for key in list(pending.keys()) if key in self._scripted]

    def close(self) -> None:
        self.closed = True


@dataclass
class Collector:
    """Captures campaign events and signals completion."""

    firsts: list = field(default_factory=list)      # (SendResult, cursor, total)
    waited: list = field(default_factory=list)      # (seconds, contacted, total)
    replies_detected: list = field(default_factory=list)  # email
    seconds: list = field(default_factory=list)     # SendResult
    stopped: list = field(default_factory=list)     # bool
    done: threading.Event = field(default_factory=threading.Event)

    def callbacks(self) -> CampaignCallbacks:
        return CampaignCallbacks(
            on_first_result=lambda r, c, t: self.firsts.append((r, c, t)),
            on_waiting=lambda s, c, t: self.waited.append((s, c, t)),
            on_reply_detected=lambda e: self.replies_detected.append(e),
            on_second_result=lambda r: self.seconds.append(r),
            on_complete=self._complete,
        )

    def _complete(self, stopped: bool) -> None:
        self.stopped.append(stopped)
        self.done.set()


def run_campaign(campaign, collector, timeout=10) -> None:
    """Start a campaign (recipients come from its state) and block until it completes."""
    assert campaign.start(collector.callbacks()), "campaign refused to start"
    assert collector.done.wait(timeout=timeout), "campaign never completed"
    if campaign._thread is not None:
        campaign._thread.join(timeout=5)
