"""Resumable campaign orchestration.

Phase 1 — Outreach: walk the saved recipient queue from its cursor. Each recipient
gets the next *available* first-message from the pool (a message under its 24h lock
is unavailable). When the pool is momentarily exhausted the campaign parks and
re-checks; because availability is derived from saved timestamps, a batch resumes
on its own once 24h has elapsed — even if the app was closed the whole time.

Phase 2 — Follow-up: throughout, poll the inbox and auto-send the single second
message, as a threaded reply, to anyone who replies.

Every state change is persisted, so closing the laptop mid-campaign and reopening
it later continues exactly where it left off.
"""

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from campaign_state import CampaignState
from gmail_client import GmailClient, SendResult, build_references, make_reply_subject
from imap_client import DetectedReply
from message_store import MessageStore

logger = logging.getLogger(__name__)

SEND_INTERVAL_SECONDS = 30
POLL_INTERVAL_SECONDS = 60
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def deduplicate_emails(emails: list[str]) -> tuple[list[str], int]:
    """Remove duplicate addresses (case-insensitive). Returns unique list and duplicate count."""
    unique: list[str] = []
    seen: set[str] = set()
    removed = 0
    for raw in emails:
        normalized = raw.strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            removed += 1
            continue
        seen.add(key)
        unique.append(normalized)
    return unique, removed


def validate_campaign(emails: list[str], store: MessageStore, needs_second: bool = True) -> list[str]:
    """Validation errors for starting a campaign."""
    errors: list[str] = []
    if not emails:
        errors.append("Recipient list is empty.")
    invalid = [e for e in emails if not EMAIL_PATTERN.match(e.strip())]
    if invalid:
        errors.append(f"Invalid address: {invalid[0]}")
    if store.usable_count() == 0:
        errors.append("Add at least one first message.")
    if needs_second and not store.second_body.strip():
        errors.append("The second (reply) message is empty.")
    return errors


@dataclass
class CampaignCallbacks:
    """UI hooks. Every field is optional; missing ones are no-ops.

    All callbacks run on the campaign's background thread — the UI layer marshals
    back to the main thread.
    """

    on_first_sending: Optional[Callable[[str], None]] = None            # (email)
    on_first_result: Optional[Callable[[SendResult, int, int], None]] = None  # (result, cursor, total)
    on_waiting: Optional[Callable[[float, int, int], None]] = None      # (seconds, contacted, total)
    on_phase_watch: Optional[Callable[[int, int], None]] = None         # (awaiting, answered)
    on_reply_detected: Optional[Callable[[str], None]] = None           # (email)
    on_second_result: Optional[Callable[[SendResult], None]] = None     # (result)
    on_complete: Optional[Callable[[bool], None]] = None                # (stopped)

    def _call(self, name: str, *args) -> None:
        callback = getattr(self, name)
        if callback is not None:
            callback(*args)


class Campaign:
    """Runs (or resumes) one persistent campaign on a background thread."""

    def __init__(
        self,
        client: GmailClient,
        store: MessageStore,
        inbox,  # imap_client.GmailInbox, or any object with find_replies(pending)
        state: CampaignState,
        interval_seconds: int = SEND_INTERVAL_SECONDS,
        poll_interval_seconds: int = POLL_INTERVAL_SECONDS,
    ):
        self.client = client
        self.store = store
        self.inbox = inbox
        self.state = state
        self.interval_seconds = interval_seconds
        self.poll_interval_seconds = poll_interval_seconds

        self._stop_requested = False
        self._resume_event = threading.Event()
        self._resume_event.set()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------ lifecycle

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def is_paused(self) -> bool:
        return not self._resume_event.is_set()

    def start(self, callbacks: Optional[CampaignCallbacks] = None) -> bool:
        """Run or resume the campaign described by the current state."""
        if self.is_running:
            return False
        if validate_campaign(self.state.emails(), self.store):
            return False
        if not self.client.is_logged_in():
            logger.error("Not signed in; cannot start campaign.")
            return False

        self.state.active = True
        self.state.save()
        self._stop_requested = False
        self._resume_event.set()
        self._thread = threading.Thread(
            target=self._run, args=(callbacks or CampaignCallbacks(),), daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        """Halt the loop. State stays active so it can be resumed later."""
        self._stop_requested = True
        self._resume_event.set()

    def pause(self) -> None:
        self._resume_event.clear()

    def resume(self) -> None:
        self._resume_event.set()

    # ------------------------------------------------------------- waiting

    def _wait_until_resumed(self) -> bool:
        while not self._resume_event.is_set():
            if self._stop_requested:
                return False
            time.sleep(0.05)
        return not self._stop_requested

    def _sleep(self, seconds: float) -> None:
        remaining = seconds
        while remaining > 0:
            if self._stop_requested:
                return
            if not self._resume_event.is_set():
                self._resume_event.wait(timeout=0.2)
                continue
            time.sleep(min(1.0, remaining))
            remaining -= 1

    # ---------------------------------------------------------------- run

    def _run(self, cb: CampaignCallbacks) -> None:
        try:
            self._loop(cb)
        finally:
            cb._call("on_complete", self._stop_requested)

    def _loop(self, cb: CampaignCallbacks) -> None:
        while not self._stop_requested:
            if not self._wait_until_resumed():
                return

            self._outreach_batch(cb)
            if self._stop_requested:
                return

            self._poll_replies(cb)

            if self.state.is_finished():
                self.state.active = False
                self.state.save()
                return

            # Not finished: either waiting for the next batch's locks to expire,
            # and/or watching for replies. Either way, re-check after a poll.
            if not self.state.outreach_complete():
                wait = self.store.seconds_until_next_available()
                if wait > 0:
                    contacted = self.state.cursor
                    cb._call("on_waiting", wait, contacted, len(self.state.recipients))

            self._sleep(self.poll_interval_seconds)

    def _outreach_batch(self, cb: CampaignCallbacks) -> None:
        """Send first messages until the pool is exhausted or the queue is done."""
        while not self._stop_requested and not self.state.outreach_complete():
            if not self._wait_until_resumed():
                return

            message = next(iter(self.store.available_first()), None)
            if message is None:
                return  # pool exhausted for now — the loop will wait and retry

            record = self.state.current()
            if record is None:
                return

            cb._call("on_first_sending", record.email)
            result = self.client.send_email(record.email, message.subject, message.body, 1)

            if result.success:
                message.mark_sent()
                self.store.save()
                self.state.mark_sent(
                    record.email, result.message_id or "", message.subject, time.time()
                )
            else:
                self.state.mark_failed(record.email)

            self.state.advance_cursor()
            cb._call("on_first_result", result, self.state.cursor, len(self.state.recipients))

            if self._stop_requested or self.state.outreach_complete():
                return
            # Pace within the batch only when another message is ready right now.
            if self.store.available_first():
                self._sleep(self.interval_seconds)

    def _poll_replies(self, cb: CampaignCallbacks) -> None:
        pending = self.state.pending_reply()
        if not pending:
            return

        answered = self.state.counts().get("Done", 0)
        cb._call("on_phase_watch", len(pending), answered)

        try:
            replies = self.inbox.find_replies(pending)
        except Exception as exc:  # inbox trouble must not kill the campaign
            logger.warning("Reply check failed: %s", exc)
            return

        second_body = self.store.second_body
        for reply in replies:
            if self._stop_requested:
                return
            self._handle_reply(reply, second_body, cb)

    def _handle_reply(self, reply: DetectedReply, second_body: str, cb: CampaignCallbacks) -> None:
        record = self.state.get(reply.email)
        if record is None or record.status not in ("Sent",):
            return  # already answered, or never contacted

        cb._call("on_reply_detected", record.email)
        subject = make_reply_subject(reply.reply_subject or record.subject)
        references = build_references(reply.reply_references, reply.reply_message_id)
        result = self.client.send_reply(
            to=record.email, body=second_body, in_reply_to=reply.reply_message_id,
            references=references, subject=subject,
        )
        if result.success:
            self.state.mark_done(record.email)
        cb._call("on_second_result", result)
