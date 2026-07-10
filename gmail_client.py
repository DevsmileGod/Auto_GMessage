"""Gmail SMTP client using an account password or app password."""

import json
import logging
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Optional

import paths
from exceptions import AuthenticationError, ConfigurationError

logger = logging.getLogger(__name__)

CREDENTIALS_PATH = paths.CREDENTIALS_PATH

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

APP_PASSWORD_HELP = (
    "Gmail rejected the password. Google no longer accepts your normal account "
    "password over SMTP — you need a 16-character App Password:\n\n"
    "1. Turn on 2-Step Verification at myaccount.google.com/security\n"
    "2. Go to myaccount.google.com/apppasswords\n"
    "3. Create a password and paste it here (spaces are fine)."
)

_RE_PREFIX = re.compile(r"^\s*re\s*:\s*", re.IGNORECASE)


def make_reply_subject(subject: str) -> str:
    """"Some subject" -> "Re: Some subject"; never stacks "Re: Re:"."""
    return "Re: " + _RE_PREFIX.sub("", subject or "").strip()


def build_references(original_references: str, replied_message_id: str) -> str:
    """Chain the References header: prior chain + the id we are replying to."""
    parts = (original_references or "").split()
    if replied_message_id and replied_message_id not in parts:
        parts.append(replied_message_id)
    return " ".join(parts)


@dataclass
class Credentials:
    """Gmail SMTP login details."""

    email: str
    app_password: str

    def validate(self) -> None:
        if not EMAIL_PATTERN.match(self.email):
            raise ConfigurationError(f"'{self.email}' is not a valid email address.")
        if not self.app_password:
            raise ConfigurationError("App password is required.")


def normalize_app_password(password: str) -> str:
    """Google displays app passwords in groups of four; the spaces are not part of it."""
    return "".join(password.split())


def load_credentials() -> Optional[Credentials]:
    """Read saved credentials, or None if absent/unreadable."""
    if not CREDENTIALS_PATH.exists():
        return None
    try:
        with CREDENTIALS_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
        email = (data.get("email") or "").strip()
        password = normalize_app_password(data.get("app_password") or "")
        if not email or not password:
            return None
        return Credentials(email=email, app_password=password)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read %s: %s", CREDENTIALS_PATH, exc)
        return None


def save_credentials(credentials: Credentials) -> None:
    """Persist credentials to a gitignored file."""
    payload = {"email": credentials.email, "app_password": credentials.app_password}
    try:
        with CREDENTIALS_PATH.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except OSError as exc:
        raise ConfigurationError(f"Could not save credentials: {exc}") from exc


def clear_credentials() -> None:
    """Delete saved credentials."""
    CREDENTIALS_PATH.unlink(missing_ok=True)


@dataclass
class SendResult:
    """Outcome of a single email send attempt."""

    email: str
    success: bool
    message_index: int = 1
    message_id: Optional[str] = None
    error: Optional[str] = None
    retry_attempt: int = 0


class GmailClient:
    """Sends mail through Gmail's SMTP server.

    The connection is reused across sends and silently re-established when Gmail
    drops it, which it does routinely on the long idle gaps between recipients.
    """

    def __init__(
        self,
        credentials: Credentials,
        host: str = SMTP_HOST,
        port: int = SMTP_PORT,
        use_starttls: bool = True,
    ):
        credentials.validate()
        self._credentials = credentials
        self._host = host
        self._port = port
        # Only the test suite's local server turns this off. Gmail always needs it.
        self._use_starttls = use_starttls
        self._smtp: Optional[smtplib.SMTP] = None
        self._authenticated = False

    @property
    def email(self) -> str:
        return self._credentials.email

    @property
    def has_connection(self) -> bool:
        """Whether a socket is currently open. Sends reconnect on demand."""
        return self._smtp is not None

    def _helo_name(self) -> str:
        """Name to greet the server with.

        Left unset, smtplib calls socket.getfqdn(), which blocks on a reverse-DNS
        lookup for every connection.
        """
        return self._credentials.email.rsplit("@", 1)[1]

    def _connect(self) -> smtplib.SMTP:
        try:
            smtp = smtplib.SMTP(
                self._host, self._port, local_hostname=self._helo_name(), timeout=30
            )
            smtp.ehlo()
            if self._use_starttls:
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
        except (OSError, smtplib.SMTPException) as exc:
            raise AuthenticationError(f"Could not reach {self._host}:{self._port} — {exc}") from exc

        try:
            smtp.login(self._credentials.email, self._credentials.app_password)
        except smtplib.SMTPAuthenticationError as exc:
            smtp.close()
            logger.error("Gmail authentication failed for %s", self._credentials.email)
            raise AuthenticationError(APP_PASSWORD_HELP) from exc
        except smtplib.SMTPException as exc:
            smtp.close()
            raise AuthenticationError(f"Gmail login failed: {exc}") from exc

        self._authenticated = True
        logger.info("Connected to Gmail SMTP as %s", self._credentials.email)
        return smtp

    def _ensure_connection(self) -> smtplib.SMTP:
        """Return a live connection, reconnecting if the old one went stale."""
        if self._smtp is not None:
            try:
                status, _ = self._smtp.noop()
                if status == 250:
                    return self._smtp
            except (smtplib.SMTPException, OSError):
                pass
            self.close()

        self._smtp = self._connect()
        return self._smtp

    def verify_login(self) -> None:
        """Open a connection to prove the credentials work. Raises AuthenticationError."""
        self.close()
        self._smtp = self._connect()

    def is_logged_in(self) -> bool:
        """Whether these credentials have been accepted by Gmail.

        Deliberately not "is a socket open": the connection is dropped and
        rebuilt freely between sends, and that must not look like a sign-out.
        """
        return self._authenticated

    def close(self) -> None:
        if self._smtp is not None:
            try:
                self._smtp.quit()
            except (smtplib.SMTPException, OSError):
                pass
            self._smtp = None

    def _build_message(
        self,
        to: str,
        subject: str,
        body: str,
        in_reply_to: Optional[str] = None,
        references: Optional[str] = None,
    ) -> EmailMessage:
        message = EmailMessage()
        message["From"] = self._credentials.email
        message["To"] = to
        message["Subject"] = subject
        message["Date"] = formatdate(localtime=True)
        # Pin the domain: make_msgid() otherwise does a reverse-DNS lookup on every
        # call, which stalls each send and puts the local hostname in the headers.
        message["Message-ID"] = make_msgid(domain=self._helo_name())
        # These two headers are what make Gmail file the message in the existing
        # conversation instead of starting a new one — i.e. a real "Reply".
        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
        if references:
            message["References"] = references
        # quoted-printable keeps the body 7-bit clean, so non-ASCII text does not
        # depend on the server advertising 8BITMIME.
        message.set_content(body, subtype="plain", charset="utf-8", cte="quoted-printable")
        return message

    def _deliver(self, message: EmailMessage, to: str, message_index: int) -> SendResult:
        """Send a pre-built message. Never raises — failure is reported in the result."""
        try:
            smtp = self._ensure_connection()
            smtp.send_message(message)
        except AuthenticationError as exc:
            logger.error("Gmail: auth failure sending to %s: %s", to, exc)
            return SendResult(email=to, success=False, message_index=message_index, error=str(exc))
        # Order matters: smtplib.SMTPException subclasses OSError, so the
        # message-level clause has to come before the socket-level one.
        except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError) as exc:
            self.close()
            logger.error("Gmail: connection lost on message %s to %s: %s", message_index, to, exc)
            return SendResult(email=to, success=False, message_index=message_index, error=str(exc))
        except smtplib.SMTPException as exc:
            # This message was refused (bad address, quota, spam block). The
            # connection is still healthy — keep it for the other recipients.
            logger.error("Gmail: message %s refused for %s: %s", message_index, to, exc)
            return SendResult(email=to, success=False, message_index=message_index, error=str(exc))
        except OSError as exc:
            # Raw socket trouble (timeout, reset). Drop it and reconnect next time.
            self.close()
            logger.error("Gmail: socket error on message %s to %s: %s", message_index, to, exc)
            return SendResult(email=to, success=False, message_index=message_index, error=str(exc))

        message_id = message["Message-ID"]
        logger.info("Gmail: message %s sent to %s (%s)", message_index, to, message_id)
        return SendResult(email=to, success=True, message_index=message_index, message_id=message_id)

    def send_email(self, to: str, subject: str, body: str, message_index: int = 1) -> SendResult:
        """Send one standalone email (subject + body). Never raises."""
        to = to.strip()
        if not EMAIL_PATTERN.match(to):
            return SendResult(
                email=to, success=False, message_index=message_index, error="Invalid recipient address"
            )
        return self._deliver(self._build_message(to, subject, body), to, message_index)

    def send_reply(
        self,
        to: str,
        body: str,
        in_reply_to: str,
        references: str,
        subject: str,
        message_index: int = 2,
    ) -> SendResult:
        """Send a body-only reply threaded onto an existing conversation.

        `in_reply_to` / `references` come from the message being replied to, so
        Gmail shows this inside that thread. `subject` should already carry the
        "Re: " prefix.
        """
        to = to.strip()
        if not EMAIL_PATTERN.match(to):
            return SendResult(
                email=to, success=False, message_index=message_index, error="Invalid recipient address"
            )
        message = self._build_message(
            to, subject, body, in_reply_to=in_reply_to, references=references
        )
        return self._deliver(message, to, message_index)


def build_client(credentials: Credentials) -> GmailClient:
    """Create a client and verify the credentials before returning it."""
    client = GmailClient(credentials)
    client.verify_login()
    return client
