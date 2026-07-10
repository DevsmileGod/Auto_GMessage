"""Resumable campaign orchestration.

Two workers run concurrently on the campaign's lifetime:

- Outreach: walk the saved recipient queue from its cursor. Each recipient gets
  the next *available* first-message from the pool (a message under its 24h lock
  is unavailable). When the pool is momentarily exhausted, outreach parks and
  re-checks; because availability is derived from saved timestamps, a batch
  resumes on its own once 24h has elapsed — even if the app was closed.

- Follow-up: poll the inbox continuously and, the moment a recipient replies,
  send the single second message back as a threaded reply — without waiting for
  outreach to finish. So a reply from recipient #5 is answered while recipient
  #15 is still being contacted.

The two workers share one SMTP connection and one state file, so SMTP sends are
serialized under `_send_lock` and all state reads/writes under `_state_lock`; the
two locks are never held at the same time. IMAP is touched only by the follow-up
worker. Every state change is persisted, so closing the laptop mid-campaign and
reopening later continues exactly where it left off.
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
        self._thread: Optional[threading.Thread] = None  # the manager thread
        self._outreach_done = threading.Event()
        self._send_lock = threading.Lock()   # serializes the shared SMTP connection
        self._state_lock = threading.Lock()  # guards state reads/writes + its file

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
        self._outreach_done.clear()
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

    # -------------------------------------------------- locked primitives

    def _send_first(self, email: str, subject: str, body: str) -> SendResult:
        with self._send_lock:
            return self.client.send_email(email, subject, body, 1)

    def _send_reply_locked(self, email, body, in_reply_to, references, subject) -> SendResult:
        with self._send_lock:
            return self.client.send_reply(
                to=email, body=body, in_reply_to=in_reply_to,
                references=references, subject=subject,
            )

    def _outreach_complete(self) -> bool:
        with self._state_lock:
            return self.state.outreach_complete()

    # ---------------------------------------------------------------- run

    def _run(self, cb: CampaignCallbacks) -> None:
        """Manager: run outreach and follow-up concurrently, then finalize once."""
        outreach = threading.Thread(target=self._outreach_worker, args=(cb,), daemon=True)
        watcher = threading.Thread(target=self._watcher_worker, args=(cb,), daemon=True)
        outreach.start()
        watcher.start()

        outreach.join()
        self._outreach_done.set()  # lets the watcher finish once no replies remain
        watcher.join()

        if not self._stop_requested:
            with self._state_lock:
                if self.state.is_finished():
                    self.state.active = False
                    self.state.save()
        cb._call("on_complete", self._stop_requested)

    # ------------------------------------------------------------- outreach

    def _outreach_worker(self, cb: CampaignCallbacks) -> None:
        """Contact recipients in batches, parking until the pool's locks expire."""
        while not self._stop_requested and not self._outreach_complete():
            if not self._wait_until_resumed():
                return

            self._outreach_batch(cb)

            if self._stop_requested or self._outreach_complete():
                return

            # Pool exhausted but recipients remain: report the wait and re-check
            # after a poll interval (availability is timestamp-derived).
            with self._state_lock:
                contacted, total = self.state.cursor, len(self.state.recipients)
            wait = self.store.seconds_until_next_available()
            if wait > 0:
                cb._call("on_waiting", wait, contacted, total)
            self._sleep(self.poll_interval_seconds)

    def _outreach_batch(self, cb: CampaignCallbacks) -> None:
        """Send first messages until the pool is exhausted or the queue is done."""
        while not self._stop_requested:
            if not self._wait_until_resumed():
                return

            message = next(iter(self.store.available_first()), None)
            if message is None:
                return  # pool exhausted for now

            with self._state_lock:
                record = self.state.current()
            if record is None:
                return

            cb._call("on_first_sending", record.email)
            result = self._send_first(record.email, message.subject, message.body)

            with self._state_lock:
                if result.success:
                    message.mark_sent()
                    self.store.save()
                    self.state.mark_sent(
                        record.email, result.message_id or "", message.subject, time.time()
                    )
                else:
                    self.state.mark_failed(record.email)
                self.state.advance_cursor()
                cursor, total = self.state.cursor, len(self.state.recipients)

            cb._call("on_first_result", result, cursor, total)

            if self._stop_requested or cursor >= total:
                return
            # Pace within the batch only when another message is ready right now.
            if self.store.available_first():
                self._sleep(self.interval_seconds)

    # -------------------------------------------------------------- follow-up

    def _watcher_worker(self, cb: CampaignCallbacks) -> None:
        """Poll for replies and answer them, concurrently with outreach."""
        while not self._stop_requested:
            if not self._wait_until_resumed():
                return

            self._poll_replies(cb)

            with self._state_lock:
                nothing_pending = not self.state.pending_reply()
            # Finish only once outreach is done AND everyone contacted has been
            # answered; otherwise keep watching (more may become pending, or a
            # reply may still arrive).
            if self._outreach_done.is_set() and nothing_pending:
                return
            self._sleep(self.poll_interval_seconds)

    def _poll_replies(self, cb: CampaignCallbacks) -> None:
        with self._state_lock:
            pending = self.state.pending_reply()
            answered = self.state.counts().get("Done", 0)
        if not pending:
            return

        cb._call("on_phase_watch", len(pending), answered)

        try:
            replies = self.inbox.find_replies(pending)  # network; never under a lock
        except Exception as exc:  # inbox trouble must not kill the campaign
            logger.warning("Reply check failed: %s", exc)
            return

        second_body = self.store.second_body
        for reply in replies:
            if self._stop_requested:
                return
            self._handle_reply(reply, second_body, cb)

    def _handle_reply(self, reply: DetectedReply, second_body: str, cb: CampaignCallbacks) -> None:
        with self._state_lock:
            record = self.state.get(reply.email)
            if record is None or record.status != "Sent":
                return  # already answered, or never contacted
            email, subject_src = record.email, record.subject

        cb._call("on_reply_detected", email)
        subject = make_reply_subject(reply.reply_subject or subject_src)
        references = build_references(reply.reply_references, reply.reply_message_id)
        result = self._send_reply_locked(
            email, second_body, reply.reply_message_id, references, subject
        )
        if result.success:
            with self._state_lock:
                self.state.mark_done(email)
        cb._call("on_second_result", result)
