"""Watch a Gmail inbox over IMAP and detect replies to messages we sent.

SMTP can only send, so detecting whether a recipient replied requires reading the
mailbox. This connects to imap.gmail.com with the same App Password and, given the
first messages we sent, finds which recipients have replied — matching primarily on
the threading headers (In-Reply-To / References) and falling back to sender+subject.
"""

import email
import email.utils
import imaplib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from exceptions import AuthenticationError
from gmail_client import Credentials

logger = logging.getLogger(__name__)

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993

IMAP_HELP = (
    "Gmail refused the IMAP connection. Two things to check:\n\n"
    "1. IMAP must be ON: Gmail → Settings → Forwarding and POP/IMAP → Enable IMAP.\n"
    "2. Use the same 16-character App Password you signed in with."
)

_RE_PREFIX = re.compile(r"^\s*re\s*:\s*", re.IGNORECASE)


@dataclass(frozen=True)
class SentInfo:
    """A first message we sent, that we are now waiting on a reply for."""

    email: str
    message_id: str  # the RFC Message-ID header we used
    subject: str
    sent_at: float  # epoch seconds


@dataclass(frozen=True)
class DetectedReply:
    """An inbound reply matched to one of our recipients."""

    email: str
    reply_message_id: str
    reply_references: str
    reply_subject: str


def _normalize_subject(subject: str) -> str:
    return _RE_PREFIX.sub("", (subject or "")).strip().lower()


def _addr(value: str) -> str:
    return email.utils.parseaddr(value or "")[1].strip().lower()


class GmailInbox:
    """A reconnecting IMAP reader over the Gmail INBOX."""

    def __init__(self, credentials: Credentials, host: str = IMAP_HOST, port: int = IMAP_PORT):
        self._credentials = credentials
        self._host = host
        self._port = port
        self._imap: Optional[imaplib.IMAP4] = None

    # ------------------------------------------------------------ connection

    def _connect(self) -> imaplib.IMAP4:
        try:
            imap = imaplib.IMAP4_SSL(self._host, self._port, timeout=30)
        except (OSError, imaplib.IMAP4.error) as exc:
            raise AuthenticationError(f"Could not reach {self._host}:{self._port} — {exc}") from exc
        try:
            imap.login(self._credentials.email, self._credentials.app_password)
            imap.select("INBOX")
        except imaplib.IMAP4.error as exc:
            try:
                imap.logout()
            except (OSError, imaplib.IMAP4.error):
                pass
            logger.error("IMAP login/select failed for %s: %s", self._credentials.email, exc)
            raise AuthenticationError(IMAP_HELP) from exc
        logger.info("IMAP connected as %s", self._credentials.email)
        return imap

    def _ensure(self) -> imaplib.IMAP4:
        if self._imap is not None:
            try:
                status, _ = self._imap.noop()
                if status == "OK":
                    return self._imap
            except (imaplib.IMAP4.error, OSError):
                pass
            self.close()
        self._imap = self._connect()
        return self._imap

    def verify_login(self) -> None:
        """Prove IMAP works before a campaign relies on it. Raises AuthenticationError."""
        self.close()
        self._imap = self._connect()

    def close(self) -> None:
        if self._imap is not None:
            try:
                self._imap.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
            self._imap = None

    # -------------------------------------------------------------- matching

    def _search_from(self, sender: str, since: datetime) -> list[bytes]:
        imap = self._ensure()
        date_str = since.strftime("%d-%b-%Y")
        typ, data = imap.search(None, "FROM", f'"{sender}"', "SINCE", date_str)
        if typ != "OK" or not data or not data[0]:
            return []
        return data[0].split()

    def _fetch_headers(self, num: bytes) -> Optional[email.message.Message]:
        imap = self._ensure()
        typ, data = imap.fetch(
            num,
            "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID IN-REPLY-TO REFERENCES SUBJECT FROM DATE)])",
        )
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            return None
        return email.message_from_bytes(data[0][1])

    def find_replies(self, pending: dict[str, SentInfo]) -> list[DetectedReply]:
        """Return replies found for the pending recipients (keyed by lowercased email).

        A match requires either the reply's In-Reply-To/References to name the
        Message-ID we sent, or the sender and normalized subject to line up.
        """
        replies: list[DetectedReply] = []
        for key, info in pending.items():
            since = datetime.fromtimestamp(info.sent_at, tz=timezone.utc) - timedelta(days=1)
            try:
                nums = self._search_from(info.email, since)
            except (imaplib.IMAP4.error, OSError) as exc:
                self.close()
                logger.warning("IMAP search failed for %s: %s", info.email, exc)
                continue

            for num in nums:
                header = self._fetch_headers(num)
                if header is None:
                    continue
                if _addr(header.get("From", "")) != key:
                    continue

                in_reply_to = header.get("In-Reply-To", "") or ""
                references = header.get("References", "") or ""
                subject = header.get("Subject", "") or ""

                header_match = info.message_id and info.message_id in (in_reply_to + " " + references)
                subject_match = _normalize_subject(subject) == _normalize_subject(info.subject)

                if header_match or subject_match:
                    replies.append(
                        DetectedReply(
                            email=info.email,
                            reply_message_id=(header.get("Message-ID", "") or "").strip(),
                            reply_references=references.strip(),
                            reply_subject=subject.strip(),
                        )
                    )
                    break
        return replies


def build_inbox(credentials: Credentials) -> GmailInbox:
    """Create an inbox reader and verify it before returning."""
    inbox = GmailInbox(credentials)
    inbox.verify_login()
    return inbox
