"""Two-message-per-recipient sending loop."""

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from gmail_client import GmailClient, SendResult

logger = logging.getLogger(__name__)

SEND_INTERVAL_SECONDS = 30
MAX_RETRY_ATTEMPTS = 3
MESSAGES_PER_RECIPIENT = 2
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

StatusCallback = Callable[[str, int], None]
ResultCallback = Callable[[SendResult, int, int], None]
CompleteCallback = Callable[[bool, list[SendResult], list[str]], None]


@dataclass
class Message:
    """One of the two messages sent to every recipient."""

    subject: str
    body: str

    def is_empty(self) -> bool:
        return not self.subject.strip() or not self.body.strip()


def deduplicate_emails(emails: list[str]) -> tuple[list[str], int]:
    """Remove duplicate addresses (case-insensitive). Returns unique list and duplicate count."""
    unique: list[str] = []
    seen: set[str] = set()
    duplicates_removed = 0

    for email in emails:
        normalized = email.strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            duplicates_removed += 1
            continue
        seen.add(key)
        unique.append(normalized)

    return unique, duplicates_removed


def validate_send_inputs(emails: list[str], messages: list[Message]) -> list[str]:
    """Return validation error messages for a send request."""
    errors: list[str] = []

    if not emails:
        errors.append("Email list is empty.")

    invalid = [e for e in emails if not EMAIL_PATTERN.match(e.strip())]
    if invalid:
        errors.append(f"Invalid address: {invalid[0]}")

    if len(messages) != MESSAGES_PER_RECIPIENT:
        errors.append(f"Exactly {MESSAGES_PER_RECIPIENT} messages are required.")

    for index, message in enumerate(messages, start=1):
        if not message.subject.strip():
            errors.append(f"Message {index}: subject is required.")
        if not message.body.strip():
            errors.append(f"Message {index}: body is empty.")

    return errors


class EmailSender:
    """Sends two messages to each recipient in turn, waiting `interval_seconds` between sends.

    The order is: recipient A message 1, wait, recipient A message 2, wait,
    recipient B message 1, wait, recipient B message 2, ... No wait after the
    final message.

    If a recipient's first message fails, the second is skipped — resending only
    the failed message on retry is what keeps a retry from delivering message 1
    twice.
    """

    def __init__(self, client: GmailClient, interval_seconds: int = SEND_INTERVAL_SECONDS):
        self.client = client
        self.interval_seconds = interval_seconds
        self._stop_requested = False
        self._resume_event = threading.Event()
        self._resume_event.set()
        self._thread: Optional[threading.Thread] = None
        self._retry_counts: dict[str, int] = {}
        # email -> message indices (1-based) still owed to that recipient
        self._pending: dict[str, list[int]] = {}

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def is_paused(self) -> bool:
        return not self._resume_event.is_set()

    def reset_session(self) -> None:
        """Clear retry tracking for a new send session."""
        self._retry_counts.clear()
        self._pending.clear()

    def get_retryable_failed(self) -> list[str]:
        """Return recipients with undelivered messages and retry attempts remaining."""
        return [
            email
            for email, indices in self._pending.items()
            if indices and self._retry_counts.get(email, 0) < MAX_RETRY_ATTEMPTS
        ]

    def pending_messages(self, email: str) -> list[int]:
        """Message indices still owed to a recipient."""
        return list(self._pending.get(email, []))

    def start(
        self,
        emails: list[str],
        messages: list[Message],
        on_status: Optional[StatusCallback] = None,
        on_result: Optional[ResultCallback] = None,
        on_complete: Optional[CompleteCallback] = None,
        is_retry: bool = False,
    ) -> bool:
        """Start the sending loop on a background thread."""
        if self.is_running:
            return False

        emails, _ = deduplicate_emails(emails)
        errors = validate_send_inputs(emails, messages)
        if errors:
            for error in errors:
                logger.error("Send validation failed: %s", error)
            return False

        if not self.client.is_logged_in():
            logger.error("Not authenticated. Sign in before starting a send.")
            return False

        if is_retry:
            for email in emails:
                self._retry_counts[email] = self._retry_counts.get(email, 0) + 1
        else:
            self.reset_session()
            for email in emails:
                self._pending[email] = list(range(1, len(messages) + 1))

        self._stop_requested = False
        self._resume_event.set()
        self._thread = threading.Thread(
            target=self._send_loop,
            args=(emails, messages, on_status, on_result, on_complete),
            daemon=True,
        )
        self._thread.start()
        return True

    def start_retry(
        self,
        emails: list[str],
        messages: list[Message],
        on_status: Optional[StatusCallback] = None,
        on_result: Optional[ResultCallback] = None,
        on_complete: Optional[CompleteCallback] = None,
    ) -> bool:
        """Resend only the messages a recipient never received."""
        return self.start(
            emails=emails,
            messages=messages,
            on_status=on_status,
            on_result=on_result,
            on_complete=on_complete,
            is_retry=True,
        )

    def stop(self) -> None:
        """Request cancellation. The in-flight send finishes first."""
        self._stop_requested = True
        self._resume_event.set()

    def pause(self) -> None:
        """Suspend the loop without losing queue position."""
        self._resume_event.clear()

    def resume(self) -> None:
        """Continue from where the loop was paused."""
        self._resume_event.set()

    def _wait_until_resumed(self) -> bool:
        """Block while paused. Returns False if stop was requested."""
        while not self._resume_event.is_set():
            if self._stop_requested:
                return False
            time.sleep(0.05)
        return not self._stop_requested

    def _sleep_interval(self) -> None:
        """Wait between sends. A pause freezes the countdown rather than consuming it."""
        remaining = self.interval_seconds
        while remaining > 0:
            if self._stop_requested:
                return
            if not self._resume_event.is_set():
                self._resume_event.wait(timeout=0.2)
                continue
            time.sleep(1)
            remaining -= 1

    def _plan(self, emails: list[str], messages: list[Message]) -> list[tuple[str, int]]:
        """Flatten the queue into (email, message_index) steps in send order."""
        steps: list[tuple[str, int]] = []
        for email in emails:
            for index in self._pending.get(email, list(range(1, len(messages) + 1))):
                steps.append((email, index))
        return steps

    def _send_loop(
        self,
        emails: list[str],
        messages: list[Message],
        on_status: Optional[StatusCallback],
        on_result: Optional[ResultCallback],
        on_complete: Optional[CompleteCallback],
    ) -> None:
        steps = self._plan(emails, messages)
        total = len(steps)
        results: list[SendResult] = []
        skip_email: Optional[str] = None

        for position, (email, message_index) in enumerate(steps):
            if self._stop_requested or not self._wait_until_resumed():
                break

            # First message failed for this recipient — don't send them the follow-up.
            if skip_email == email:
                continue
            skip_email = None

            if on_status:
                on_status(email, message_index)

            message = messages[message_index - 1]
            result = self.client.send_email(email, message.subject, message.body, message_index)
            result.retry_attempt = self._retry_counts.get(email, 0)
            results.append(result)

            if result.success:
                owed = self._pending.get(email)
                if owed and message_index in owed:
                    owed.remove(message_index)
            else:
                skip_email = email

            if on_result:
                on_result(result, position + 1, total)

            if self._stop_requested:
                break

            # Wait only if another message is actually going out after this one.
            has_more = any(e != skip_email for e, _ in steps[position + 1 :])
            if not has_more:
                break

            self._sleep_interval()

        if on_complete:
            retryable = [] if self._stop_requested else self.get_retryable_failed()
            on_complete(self._stop_requested, results, retryable)
