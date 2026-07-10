"""Persistent campaign progress — the resume flag.

The recipient queue, a cursor marking the next person to contact, and each
recipient's status are all saved to disk after every step. Nothing here relies on
a running timer: whether a laptop slept for five minutes or five hours, the app
reloads this file on launch and continues from the cursor. Combined with the
per-message 24h lock timestamps in messages.json, that is enough to drip a large
list in daily batches across shutdowns.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import paths
from imap_client import SentInfo

logger = logging.getLogger(__name__)

STATE_PATH = paths.BASE_DIR / "campaign_state.json"

STATUS_PENDING = "Pending"
STATUS_SENT = "Sent"        # first message delivered, awaiting a reply
STATUS_FAILED = "Failed"    # first message could not be sent
STATUS_DONE = "Done"        # replied and follow-up delivered

# Statuses that still owe a reply-watch (rebuilt into the pending set on resume).
AWAITING_STATUSES = {STATUS_SENT}


@dataclass
class RecipientRecord:
    email: str
    status: str = STATUS_PENDING
    message_id: str = ""    # RFC Message-ID of the first message we sent them
    subject: str = ""       # subject of that first message (for reply threading)
    sent_at: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "email": self.email,
            "status": self.status,
            "message_id": self.message_id,
            "subject": self.subject,
            "sent_at": self.sent_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RecipientRecord":
        return cls(
            email=data.get("email", ""),
            status=data.get("status", STATUS_PENDING),
            message_id=data.get("message_id", ""),
            subject=data.get("subject", ""),
            sent_at=data.get("sent_at"),
        )


class CampaignState:
    """The saved state of one in-progress (or idle) campaign."""

    def __init__(self, path: Path = STATE_PATH):
        self._path = path
        self.recipients: list[RecipientRecord] = []
        self.cursor: int = 0      # index of the next recipient to attempt outreach
        self.active: bool = False  # a started campaign that has not finished/been reset
        self.load()

    # ----------------------------------------------------------- persistence

    def load(self) -> None:
        if not self._path.exists():
            return
        try:
            with self._path.open(encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read %s: %s", self._path, exc)
            return
        self.recipients = [RecipientRecord.from_dict(r) for r in data.get("recipients", [])]
        self.cursor = int(data.get("cursor", 0))
        self.active = bool(data.get("active", False))

    def save(self) -> None:
        payload = {
            "active": self.active,
            "cursor": self.cursor,
            "recipients": [r.to_dict() for r in self.recipients],
        }
        try:
            with self._path.open("w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
        except OSError as exc:
            logger.error("Could not save %s: %s", self._path, exc)

    # -------------------------------------------------------- queue editing

    def emails(self) -> list[str]:
        return [r.email for r in self.recipients]

    def _key(self, email: str) -> str:
        return email.strip().lower()

    def get(self, email: str) -> Optional[RecipientRecord]:
        key = self._key(email)
        return next((r for r in self.recipients if self._key(r.email) == key), None)

    def add_emails(self, emails: list[str]) -> int:
        """Add new recipients (deduped against the existing queue). Returns count added."""
        existing = {self._key(r.email) for r in self.recipients}
        added = 0
        for email in emails:
            key = self._key(email)
            if not email.strip() or key in existing:
                continue
            existing.add(key)
            self.recipients.append(RecipientRecord(email=email.strip()))
            added += 1
        if added:
            self.save()
        return added

    def remove_emails(self, emails: list[str]) -> None:
        keys = {self._key(e) for e in emails}
        self.recipients = [r for r in self.recipients if self._key(r.email) not in keys]
        self.save()

    def clear(self) -> None:
        """Abandon any campaign and empty the queue."""
        self.recipients = []
        self.cursor = 0
        self.active = False
        self.save()

    def begin(self) -> None:
        """Start a fresh campaign: reset everyone to Pending, cursor to 0, active."""
        for record in self.recipients:
            record.status = STATUS_PENDING
            record.message_id = ""
            record.subject = ""
            record.sent_at = None
        self.cursor = 0
        self.active = True
        self.save()

    # ---------------------------------------------------------------- flags

    def mark_sent(self, email: str, message_id: str, subject: str, sent_at: float) -> None:
        record = self.get(email)
        if record:
            record.status = STATUS_SENT
            record.message_id = message_id
            record.subject = subject
            record.sent_at = sent_at
            self.save()

    def mark_failed(self, email: str) -> None:
        record = self.get(email)
        if record:
            record.status = STATUS_FAILED
            self.save()

    def mark_done(self, email: str) -> None:
        record = self.get(email)
        if record:
            record.status = STATUS_DONE
            self.save()

    def advance_cursor(self) -> None:
        self.cursor = min(self.cursor + 1, len(self.recipients))
        self.save()

    # --------------------------------------------------------------- queries

    def current(self) -> Optional[RecipientRecord]:
        """The recipient the cursor points at (next to contact), or None if done."""
        if 0 <= self.cursor < len(self.recipients):
            return self.recipients[self.cursor]
        return None

    def outreach_complete(self) -> bool:
        return self.cursor >= len(self.recipients)

    def pending_reply(self) -> dict[str, SentInfo]:
        """Contacted recipients still awaiting a reply, keyed by lowercased email."""
        out: dict[str, SentInfo] = {}
        for record in self.recipients:
            if record.status in AWAITING_STATUSES and record.message_id:
                out[self._key(record.email)] = SentInfo(
                    email=record.email,
                    message_id=record.message_id,
                    subject=record.subject,
                    sent_at=record.sent_at or 0.0,
                )
        return out

    def is_finished(self) -> bool:
        return self.outreach_complete() and not self.pending_reply()

    def has_resumable_work(self) -> bool:
        return self.active and (not self.outreach_complete() or bool(self.pending_reply()))

    def counts(self) -> dict[str, int]:
        c = {STATUS_PENDING: 0, STATUS_SENT: 0, STATUS_FAILED: 0, STATUS_DONE: 0}
        for record in self.recipients:
            c[record.status] = c.get(record.status, 0) + 1
        return c
