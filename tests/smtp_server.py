"""A minimal in-process SMTP server for end-to-end tests.

Speaks just enough of RFC 5321 for smtplib: EHLO, AUTH PLAIN, MAIL, RCPT,
DATA, NOOP, RSET, QUIT. No TLS — tests point the client at it with
use_starttls=False.
"""

import base64
import email.policy
import socketserver
import threading
import time
from dataclasses import dataclass, field
from email import message_from_bytes
from email.message import EmailMessage


@dataclass
class ReceivedEmail:
    """One message as the server actually received it off the wire."""

    mail_from: str
    rcpt_to: list[str]
    raw_bytes: bytes
    received_at: float

    @property
    def raw(self) -> str:
        return self.raw_bytes.decode("utf-8", "replace")

    @property
    def parsed(self) -> EmailMessage:
        # Parse from bytes, not str: parsing 8-bit content from a str runs it
        # through raw-unicode-escape and mangles non-ASCII into \uXXXX literals.
        # policy=default also decodes RFC 2047 headers and quoted-printable bodies.
        return message_from_bytes(self.raw_bytes, policy=email.policy.default)

    @property
    def subject(self) -> str:
        return self.parsed["Subject"]

    @property
    def to(self) -> str:
        return self.parsed["To"]

    @property
    def body(self) -> str:
        return self.parsed.get_content().strip()


@dataclass
class Mailbox:
    """Shared state between the server thread and the test."""

    emails: list[ReceivedEmail] = field(default_factory=list)
    username: str = "sender@gmail.com"
    password: str = "abcdefghijklmnop"
    access_token: str = "ya29.test-access-token"
    reject_auth: bool = False
    reject_recipients: set[str] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, email: ReceivedEmail) -> None:
        with self.lock:
            self.emails.append(email)


class _Handler(socketserver.StreamRequestHandler):
    mailbox: Mailbox

    def _send(self, line: str) -> None:
        self.wfile.write(f"{line}\r\n".encode())
        self.wfile.flush()

    def _read(self) -> str:
        return self.rfile.readline().decode("utf-8", "replace").strip()

    def _check_plain(self, payload: str) -> bool:
        try:
            _, user, password = base64.b64decode(payload).decode().split("\0")
        except (ValueError, UnicodeDecodeError):
            return False
        return (
            not self.mailbox.reject_auth
            and user == self.mailbox.username
            and password == self.mailbox.password
        )

    def _check_xoauth2(self, payload: str) -> bool:
        """Decode `user=<email>^Aauth=Bearer <token>^A^A`, as Gmail does."""
        try:
            decoded = base64.b64decode(payload).decode()
        except (ValueError, UnicodeDecodeError):
            return False
        fields = dict(
            part.split("=", 1) for part in decoded.split("\x01") if "=" in part
        )
        return (
            not self.mailbox.reject_auth
            and fields.get("user") == self.mailbox.username
            and fields.get("auth") == f"Bearer {self.mailbox.access_token}"
        )

    def handle(self) -> None:
        self._send("220 localhost ESMTP test")
        authenticated = False
        mail_from = ""
        rcpt_to: list[str] = []

        while True:
            line = self._read()
            if not line:
                return
            command, _, rest = line.partition(" ")
            command = command.upper()

            if command in ("EHLO", "HELO"):
                self._send("250-localhost")
                self._send("250-AUTH PLAIN XOAUTH2")
                self._send("250 HELP")

            elif command == "AUTH":
                mechanism, _, payload = rest.partition(" ")
                mechanism = mechanism.upper()
                if mechanism == "PLAIN":
                    accepted = self._check_plain(payload)
                elif mechanism == "XOAUTH2":
                    accepted = self._check_xoauth2(payload)
                else:
                    self._send("504 Unrecognized authentication type")
                    continue

                if accepted:
                    authenticated = True
                    self._send("235 2.7.0 Accepted")
                else:
                    self._send("535 5.7.8 Username and Password not accepted")

            elif command == "MAIL":
                if not authenticated:
                    self._send("530 5.7.0 Authentication Required")
                    continue
                mail_from = rest.partition(":")[2].strip().strip("<>")
                rcpt_to = []
                self._send("250 OK")

            elif command == "RCPT":
                address = rest.partition(":")[2].strip().strip("<>")
                if address in self.mailbox.reject_recipients:
                    self._send("550 5.1.1 No such user")
                    continue
                rcpt_to.append(address)
                self._send("250 OK")

            elif command == "DATA":
                self._send("354 End data with <CR><LF>.<CR><LF>")
                lines: list[bytes] = []
                while True:
                    data_line = self.rfile.readline()
                    if data_line.rstrip(b"\r\n") == b".":
                        break
                    if data_line.startswith(b".."):  # undo transparency dot-stuffing
                        data_line = data_line[1:]
                    lines.append(data_line.rstrip(b"\r\n"))

                self.mailbox.record(
                    ReceivedEmail(
                        mail_from=mail_from,
                        rcpt_to=list(rcpt_to),
                        raw_bytes=b"\n".join(lines),
                        received_at=time.monotonic(),
                    )
                )
                self._send("250 2.0.0 OK: queued")

            elif command == "NOOP":
                self._send("250 OK")

            elif command == "RSET":
                mail_from, rcpt_to = "", []
                self._send("250 OK")

            elif command == "QUIT":
                self._send("221 Bye")
                return

            else:
                self._send("502 Command not implemented")

    def handle_error(self, *_args) -> None:
        pass


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        pass  # A client that hangs up mid-conversation is expected in these tests.


class SMTPTestServer:
    """Run the test SMTP server on an ephemeral port."""

    def __init__(self, mailbox: Mailbox):
        self.mailbox = mailbox
        handler = type("BoundHandler", (_Handler,), {"mailbox": mailbox})
        self._server = _Server(("127.0.0.1", 0), handler)
        self.host, self.port = self._server.server_address
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "SMTPTestServer":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
