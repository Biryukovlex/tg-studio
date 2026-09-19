"""Encryption for persisted Telethon sessions and API hashes.

The key is supplied by the deployment environment, never generated into the
database or sent to the browser.  Fernet provides authenticated encryption and
a key fingerprint is stored alongside the ciphertext so rotation can detect
rows that still use a previous key.

Generate a key with ``scripts/generate_session_key.py``.  Raw passphrases are
rejected: use a 32-byte urlsafe-base64 Fernet key.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


def _strict_fernet_key(value: str | bytes, *, kind: str) -> bytes:
    raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    raw = raw.strip()
    hint = "generate one with `scripts/generate_session_key.py`"
    if not raw:
        raise ValueError(f"{kind} encryption key is empty; {hint}")
    try:
        decoded = base64.urlsafe_b64decode(raw)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ValueError(f"{kind} encryption key is not valid urlsafe-base64; {hint}") from exc
    if len(decoded) != 32:
        raise ValueError(f"{kind} encryption key must decode to 32 bytes; {hint}")
    return raw


def key_fingerprint(encoded_key: bytes) -> str:
    return "v2:" + hashlib.sha256(encoded_key).hexdigest()[:16]


class SessionCipher:
    """Fernet cipher with optional previous-key support for rotation."""

    def __init__(self, key: str | bytes, previous_key: str | bytes | None = None) -> None:
        self._encoded = _strict_fernet_key(key, kind="current")
        self._fernet = Fernet(self._encoded)
        self._previous: Fernet | None = None
        if previous_key:
            previous_raw = previous_key.encode("utf-8") if isinstance(previous_key, str) else bytes(previous_key)
            if previous_raw.strip():
                self._previous = Fernet(_strict_fernet_key(previous_raw, kind="previous"))
        self.key_version = key_fingerprint(self._encoded)
        readers = [self._fernet] + ([self._previous] if self._previous else [])
        self._reader = MultiFernet(readers)

    def encrypt(self, session_string: str) -> bytes:
        if not isinstance(session_string, str) or not session_string:
            raise ValueError("session string must be a non-empty string")
        return self._fernet.encrypt(session_string.encode("utf-8"))

    def decrypt(self, token: bytes) -> str:
        """Decrypt with the current key, falling back to the previous key."""
        try:
            return self._reader.decrypt(bytes(token)).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise ValueError("Telegram session could not be decrypted with the configured key") from exc

    def decrypt_with_previous_flag(self, token: bytes) -> tuple[str, bool]:
        """Return (plaintext, used_previous_key)."""
        raw = bytes(token)
        try:
            return self._fernet.decrypt(raw).decode("utf-8"), False
        except (InvalidToken, UnicodeDecodeError):
            pass
        if self._previous is not None:
            try:
                return self._previous.decrypt(raw).decode("utf-8"), True
            except (InvalidToken, UnicodeDecodeError):
                pass
        # Keep the error path identical to decrypt() for callers.
        return self.decrypt(raw), False


def build_cipher(value: str, previous: str | None = None) -> SessionCipher | None:
    value = (value or "").strip()
    if not value:
        return None
    if previous is None:
        # Rotation seam: operators set TELEGRAM_SESSION_ENCRYPTION_KEY_PREVIOUS
        # to the old key while rows are being re-encrypted. Explicit callers
        # may also pass it directly.
        previous = os.environ.get("TELEGRAM_SESSION_ENCRYPTION_KEY_PREVIOUS", "")
    previous_value = (previous or "").strip() or None
    return SessionCipher(value, previous_key=previous_value)
