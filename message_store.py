"""Message storage: a rotating pool of first messages and a single reply message.

The first-message pool is a list of distinct emails (subject + body). Each is
sent to at most one recipient, then locked for 24 hours so the same text is never
reused across a campaign or across restarts — the lock timestamp is persisted.

The second message is a single body (no subject); it is sent as a threaded reply,
so its subject is derived from the first message ("Re: ...") at send time.
"""

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import paths

logger = logging.getLogger(__name__)

MESSAGES_PATH = paths.BASE_DIR / "messages.json"

COOLDOWN_SECONDS = 24 * 60 * 60  # a sent first message is locked for 24 hours


def _now() -> float:
    return time.time()


@dataclass
class FirstMessage:
    """One message in the rotating first-message pool."""

    subject: str
    body: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    last_sent_at: Optional[float] = None

    def is_empty(self) -> bool:
        return not self.subject.strip() or not self.body.strip()

    def cooldown_remaining(self, now: Optional[float] = None) -> float:
        """Seconds until this message is available again (0 if available now)."""
        if self.last_sent_at is None:
            return 0.0
        elapsed = (now if now is not None else _now()) - self.last_sent_at
        return max(0.0, COOLDOWN_SECONDS - elapsed)

    def is_available(self, now: Optional[float] = None) -> bool:
        return self.cooldown_remaining(now) <= 0

    def mark_sent(self, now: Optional[float] = None) -> None:
        self.last_sent_at = now if now is not None else _now()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "subject": self.subject,
            "body": self.body,
            "last_sent_at": self.last_sent_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FirstMessage":
        return cls(
            id=data.get("id") or uuid.uuid4().hex,
            subject=data.get("subject", ""),
            body=data.get("body", ""),
            last_sent_at=data.get("last_sent_at"),
        )


class MessageStore:
    """The first-message pool and the second message, with JSON persistence."""

    def __init__(self, path: Path = MESSAGES_PATH):
        self._path = path
        self.first_pool: list[FirstMessage] = []
        self.second_body: str = ""
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

        self.first_pool = [
            FirstMessage.from_dict(item) for item in data.get("first_pool", [])
        ]
        self.second_body = data.get("second_message", {}).get("body", "")

    def save(self) -> None:
        payload = {
            "first_pool": [m.to_dict() for m in self.first_pool],
            "second_message": {"body": self.second_body},
        }
        try:
            with self._path.open("w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
        except OSError as exc:
            logger.error("Could not save %s: %s", self._path, exc)
            raise

    # ------------------------------------------------------------- first CRUD

    def get_first(self, message_id: str) -> Optional[FirstMessage]:
        return next((m for m in self.first_pool if m.id == message_id), None)

    def add_first(self, subject: str, body: str) -> FirstMessage:
        message = FirstMessage(subject=subject.strip(), body=body.strip())
        self.first_pool.append(message)
        self.save()
        return message

    def update_first(self, message_id: str, subject: str, body: str) -> bool:
        message = self.get_first(message_id)
        if message is None:
            return False
        message.subject = subject.strip()
        message.body = body.strip()
        self.save()
        return True

    def delete_first(self, message_id: str) -> bool:
        message = self.get_first(message_id)
        if message is None:
            return False
        self.first_pool.remove(message)
        self.save()
        return True

    def reset_cooldowns(self) -> None:
        """Clear all 24h locks (manual override)."""
        for message in self.first_pool:
            message.last_sent_at = None
        self.save()

    # ---------------------------------------------------------------- second

    def set_second(self, body: str) -> None:
        self.second_body = body.strip()
        self.save()

    def clear_second(self) -> None:
        self.second_body = ""
        self.save()

    # -------------------------------------------------------------- rotation

    def available_first(self, now: Optional[float] = None) -> list[FirstMessage]:
        """Non-empty, non-locked pool messages, in list order."""
        return [m for m in self.first_pool if not m.is_empty() and m.is_available(now)]

    def available_count(self, now: Optional[float] = None) -> int:
        return len(self.available_first(now))

    def usable_count(self) -> int:
        """Non-empty messages, regardless of lock — the ceiling on batch size."""
        return sum(1 for m in self.first_pool if not m.is_empty())

    def seconds_until_next_available(self, now: Optional[float] = None) -> float:
        """0 if a message is ready now; else seconds until the soonest lock expires.

        Derived purely from saved timestamps, so time spent with the app closed
        counts toward the wait — there is no live timer to lose.
        """
        if self.available_first(now):
            return 0.0
        locked = [m for m in self.first_pool if not m.is_empty()]
        if not locked:
            return 0.0
        return min(m.cooldown_remaining(now) for m in locked)
