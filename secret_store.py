"""Encrypt saved credentials at rest.

The app remembers either an App Password or a Google refresh token, and both are
long-lived keys to the user's mailbox. Writing them to a plain JSON file means anyone
who can read the folder — a backup, a synced drive, another program — has the account.

On Windows we hand the secret to DPAPI (CryptProtectData), which encrypts it with a key
derived from the logged-in user's account. The ciphertext is worthless on another
machine or under another user, and we never manage a key ourselves.

Elsewhere there is no equivalent without a dependency, so the value is stored as-is and
marked accordingly — the reader must not care which form it finds.
"""

import base64
import ctypes
import logging
import sys
from ctypes import wintypes
from typing import Optional

logger = logging.getLogger(__name__)

DPAPI_PREFIX = "dpapi:"

_IS_WINDOWS = sys.platform == "win32"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    @classmethod
    def of(cls, data: bytes) -> "_Blob":
        buffer = ctypes.create_string_buffer(data, len(data))
        return cls(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))

    def value(self) -> bytes:
        return ctypes.string_at(self.pbData, self.cbData)


def _crypt(func_name: str, data: bytes) -> Optional[bytes]:
    """Call CryptProtectData / CryptUnprotectData. None if DPAPI is unavailable or fails."""
    if not _IS_WINDOWS:
        return None
    try:
        crypt32 = ctypes.WinDLL("crypt32.dll")
        kernel32 = ctypes.WinDLL("kernel32.dll")
        func = getattr(crypt32, func_name)
    except (OSError, AttributeError) as exc:
        logger.warning("DPAPI unavailable (%s): %s", func_name, exc)
        return None

    source = _Blob.of(data)
    result = _Blob()
    # The trailing args are description, reserved, prompt-struct and flags; all unused.
    ok = func(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(result))
    if not ok:
        logger.warning("%s failed (error %s)", func_name, ctypes.get_last_error())
        return None
    try:
        return result.value()
    finally:
        kernel32.LocalFree(result.pbData)


def protect(secret: str) -> str:
    """Encrypt a secret for storage. Falls back to the plain value off Windows."""
    if not secret:
        return ""
    encrypted = _crypt("CryptProtectData", secret.encode("utf-8"))
    if encrypted is None:
        return secret
    return DPAPI_PREFIX + base64.b64encode(encrypted).decode("ascii")


def unprotect(stored: str) -> str:
    """Decrypt a stored secret. Values written before encryption existed pass through."""
    if not stored:
        return ""
    if not stored.startswith(DPAPI_PREFIX):
        # Either a pre-encryption credentials file or a non-Windows one. Both are
        # already the secret itself.
        return stored
    try:
        raw = base64.b64decode(stored[len(DPAPI_PREFIX):], validate=True)
    except (ValueError, TypeError) as exc:
        logger.warning("Stored secret is not valid base64: %s", exc)
        return ""
    decrypted = _crypt("CryptUnprotectData", raw)
    if decrypted is None:
        # Encrypted by a different Windows user or on a different machine — we cannot
        # recover it, and the caller should treat that as "not signed in".
        logger.warning("Could not decrypt the saved credential; a fresh sign-in is needed.")
        return ""
    return decrypted.decode("utf-8", "replace")
