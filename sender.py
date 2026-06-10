"""Email sending logic."""

import base64
import logging
import re
import threading
import time
from dataclasses import dataclass
from email.mime.text import MIMEText
from typing import Callable

from googleapiclient.discovery import Resource


logger = logging.getLogger(__name__)

SEND_INTERVAL_SECONDS = 30
MAX_RETRY_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 2
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


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


def validate_send_inputs(emails: list[str], subject: str, body: str) -> list[str]:
    """Return validation error messages for a send request."""
    errors: list[str] = []

    if not emails:
        errors.append("Email list is empty.")
    if not subject.strip():
        errors.append("Subject is required.")
    if not body.strip():
        errors.append("Message body is empty.")

    return errors


@dataclass
class SendResult:
    """Outcome of a single email send attempt."""

    email: str
    success: bool
    message_id: str | None = None
    error: str | None = None
    retry_attempt: int = 0


class EmailSender:
    """Sends emails via Gmail API on a background thread with a fixed interval."""

    def __init__(self, interval_seconds: int = SEND_INTERVAL_SECONDS):
        self.interval_seconds = interval_seconds
        self._stop_requested = False
        self._resume_event = threading.Event()
        self._resume_event.set()
        self._thread: threading.Thread | None = None
        self._retry_counts: dict[str, int] = {}
        self._session_failed: list[str] = []

    @staticmethod
    def send_email(
        service: Resource,
        to: str,
        subject: str,
        body: str,
        sender: str | None = None,
    ) -> SendResult:
        """Encode an RFC 2822 message as base64 and send via Gmail API."""
        try:
            message = MIMEText(body)
            message["to"] = to
            message["subject"] = subject
            if sender:
                message["from"] = sender

            raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
            payload = {"raw": raw}

            result = service.users().messages().send(userId="me", body=payload).execute()
            message_id = result.get("id")
            logger.info("Email sent to %s (message id: %s)", to, message_id)
            return SendResult(email=to, success=True, message_id=message_id)
        except Exception as exc:
            logger.error("Failed to send email to %s: %s", to, exc)
            return SendResult(email=to, success=False, error=str(exc))

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def is_paused(self) -> bool:
        return not self._resume_event.is_set()

    def reset_session(self) -> None:
        """Clear failed-email tracking for a new send session."""
        self._retry_counts.clear()
        self._session_failed.clear()

    def get_retryable_failed(self) -> list[str]:
        """Return failed emails that still have retry attempts remaining."""
        return [
            email
            for email in dict.fromkeys(self._session_failed)
            if self._retry_counts.get(email, 0) < MAX_RETRY_ATTEMPTS
        ]

    def start(
        self,
        emails: list[str],
        subject: str,
        body: str,
        on_status: Callable[[str], None] | None = None,
        on_result: Callable[[SendResult, int, int], None] | None = None,
        on_complete: Callable[[bool, list[SendResult], list[str]], None] | None = None,
        is_retry: bool = False,
        gmail_service: Resource | None = None,
    ) -> bool:
        """Start the sending loop in a background thread."""
        if self.is_running:
            return False

        emails, _ = deduplicate_emails(emails)
        errors = validate_send_inputs(emails, subject, body)
        if errors:
            for error in errors:
                logger.error("Send validation failed: %s", error)
            return False

        if gmail_service is None:
            logger.error("Gmail service not provided. Authenticate before starting send.")
            return False

        if not is_retry:
            self.reset_session()

        self._stop_requested = False
        self._resume_event.set()
        self._thread = threading.Thread(
            target=self._send_loop,
            args=(
                gmail_service,
                emails,
                subject,
                body,
                on_status,
                on_result,
                on_complete,
                is_retry,
            ),
            daemon=True,
        )
        self._thread.start()
        return True

    def start_retry(
        self,
        emails: list[str],
        subject: str,
        body: str,
        on_status: Callable[[str], None] | None = None,
        on_result: Callable[[SendResult, int, int], None] | None = None,
        on_complete: Callable[[bool, list[SendResult], list[str]], None] | None = None,
        gmail_service: Resource | None = None,
    ) -> bool:
        """Re-queue and resend failed emails."""
        return self.start(
            emails=emails,
            subject=subject,
            body=body,
            on_status=on_status,
            on_result=on_result,
            on_complete=on_complete,
            is_retry=True,
            gmail_service=gmail_service,
        )

    def stop(self) -> None:
        """Safely request cancellation of the sending loop."""
        self._stop_requested = True
        self._resume_event.set()

    def pause(self) -> None:
        """Suspend the sending loop without losing queue position."""
        self._resume_event.clear()

    def resume(self) -> None:
        """Continue sending from where the loop was paused."""
        self._resume_event.set()

    def _wait_until_resumed(self) -> bool:
        """Block while paused. Returns False if stop was requested."""
        while not self._resume_event.is_set():
            if self._stop_requested:
                return False
            time.sleep(0.1)
        return not self._stop_requested

    def _sleep_seconds(self, seconds: int) -> None:
        """Sleep for the given duration, honoring pause and stop."""
        elapsed = 0
        while elapsed < seconds:
            if self._stop_requested:
                return

            # Paused: wait without consuming interval time
            if not self._resume_event.is_set():
                self._resume_event.wait(timeout=0.2)
                continue

            time.sleep(1)
            elapsed += 1

    def _sleep_interval(self) -> None:
        """Wait between sends, pausing without losing remaining delay."""
        self._sleep_seconds(self.interval_seconds)

    def _apply_retry_backoff(self, retry_attempt: int) -> None:
        """Exponential backoff before a retry attempt."""
        delay = BACKOFF_BASE_SECONDS * (2 ** (retry_attempt - 1))
        logger.info("Retry backoff for attempt %s: %ss", retry_attempt, delay)
        self._sleep_seconds(delay)

    def _record_failure(self, email: str) -> None:
        if email not in self._session_failed:
            self._session_failed.append(email)

    def _record_success(self, email: str) -> None:
        if email in self._session_failed:
            self._session_failed.remove(email)
        self._retry_counts.pop(email, None)

    def _send_loop(
        self,
        service: Resource,
        emails: list[str],
        subject: str,
        body: str,
        on_status: Callable[[str], None] | None,
        on_result: Callable[[SendResult, int, int], None] | None,
        on_complete: Callable[[bool, list[SendResult], list[str]], None] | None,
        is_retry: bool,
    ) -> None:
        total = len(emails)
        results: list[SendResult] = []

        for index, email in enumerate(emails):
            if self._stop_requested:
                break

            if not self._wait_until_resumed():
                break

            retry_attempt = 0
            if is_retry:
                retry_attempt = self._retry_counts.get(email, 0) + 1
                self._apply_retry_backoff(retry_attempt)
                if self._stop_requested:
                    break
                if not self._wait_until_resumed():
                    break

            if on_status:
                on_status(email)

            result = self.send_email(service, email, subject, body)
            result.retry_attempt = retry_attempt
            results.append(result)

            if result.success:
                self._record_success(email)
            elif is_retry:
                self._retry_counts[email] = retry_attempt
                if self._retry_counts[email] < MAX_RETRY_ATTEMPTS:
                    self._record_failure(email)
            else:
                self._record_failure(email)

            if on_result:
                on_result(result, index + 1, total)

            if self._stop_requested or index >= total - 1:
                break

            if not is_retry:
                self._sleep_interval()

        if on_complete:
            retryable = [] if self._stop_requested else self.get_retryable_failed()
            on_complete(self._stop_requested, results, retryable)
