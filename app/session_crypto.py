"""Encryption for persisted Telethon sessions.

The key is supplied by the deployment environment, never generated into the
database or sent to the browser.  Fernet provides authenticated encryption and
key-version metadata is stored alongside the ciphertext for future rotation.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


class SessionCipher:
    key_version = "v1"

    def __init__(self, key: str | bytes) -> None:
        raw = key.encode("utf-8") if isinstance(key, str) else bytes(key)
        try:
            decoded = base64.urlsafe_b64decode(raw)
            if len(decoded) != 32:
                raise ValueError
            encoded = raw
        except (ValueError, TypeError, base64.binascii.Error):
            # A deployment may provide a high-entropy raw secret.  Derive a
            # Fernet-sized key without ever storing the raw input.
            encoded = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
        self._fernet = Fernet(encoded)

    def encrypt(self, session_string: str) -> bytes:
        if not isinstance(session_string, str) or not session_string:
            raise ValueError("session string must be a non-empty string")
        return self._fernet.encrypt(session_string.encode("utf-8"))

    def decrypt(self, token: bytes) -> str:
        try:
            return self._fernet.decrypt(bytes(token)).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise ValueError("Telegram session could not be decrypted with the configured key") from exc


def build_cipher(value: str) -> SessionCipher | None:
    value = (value or "").strip()
    return SessionCipher(value) if value else None
