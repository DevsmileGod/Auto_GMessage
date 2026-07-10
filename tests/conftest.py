"""Shared test fixtures."""

import sys
import threading
import tkinter as tk
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gmail_client import SendResult  # noqa: E402


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


class FakeGmailClient:
    """Records every send instead of touching the network."""

    def __init__(self, fail_on: set[tuple[str, int]] | None = None):
        self.sent: list[tuple[str, int]] = []
        self.fail_on = fail_on or set()
        self.email = "sender@gmail.com"
        self._lock = threading.Lock()

    def is_logged_in(self) -> bool:
        return True

    def close(self) -> None:
        pass

    def send_email(self, to: str, subject: str, body: str, message_index: int = 1) -> SendResult:
        with self._lock:
            self.sent.append((to, message_index))
        if (to, message_index) in self.fail_on:
            return SendResult(
                email=to, success=False, message_index=message_index, error="simulated failure"
            )
        return SendResult(
            email=to, success=True, message_index=message_index, message_id=f"<{to}-{message_index}>"
        )


@pytest.fixture
def no_sleep(monkeypatch):
    """Make interval waits instant while recording how long each one would have been."""
    import sender

    waits: list[int] = []

    def fake_sleep_interval(self) -> None:
        if self._stop_requested:
            return
        waits.append(self.interval_seconds)

    monkeypatch.setattr(sender.EmailSender, "_sleep_interval", fake_sleep_interval)
    return waits


def run_to_completion(email_sender, timeout: float = 5.0) -> None:
    """Block until the background send thread finishes."""
    thread = email_sender._thread
    assert thread is not None, "send thread was never started"
    thread.join(timeout=timeout)
    assert not thread.is_alive(), "send thread did not finish in time"
