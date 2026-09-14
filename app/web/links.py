"""Canonical links for Telegram channels and messages.

Channel identifiers arrive from environment variables, the settings form, and
Telegram itself.  Keeping normalization and URL construction in one small
module prevents the dashboard and Saved Messages commands from drifting apart.
"""

from __future__ import annotations

import re
from typing import Any


_USERNAME_RE = re.compile(r"[A-Za-z0-9_]+")
_NUMERIC_CHAT_RE = re.compile(r"-?\d+")


def normalize_channel_identifier(value: Any) -> str:
    """Return the canonical stored form for a supported channel identifier.

    Public usernames are stored as ``@name``.  Numeric Telegram channel IDs
    (including the ``-100`` supergroup prefix) are preserved exactly so they
    remain resolvable by Telethon and can be mapped to ``t.me/c`` links later.
    Unknown values are returned trimmed for the caller's existing validation to
    reject; this helper does not silently invent a different channel.
    """

    raw = str(value or "").strip()
    if not raw:
        return ""
    lowered = raw.lower()
    if lowered.startswith(("https://t.me/", "http://t.me/", "t.me/")):
        path = raw.split("t.me/", 1)[1].split("/", 1)[0].split("?", 1)[0].strip()
        if path and _USERNAME_RE.fullmatch(path):
            return f"@{path}"
        return raw
    if raw.startswith("@"):
        name = raw[1:].strip()
        return f"@{name}" if name else ""
    if raw.startswith("-100") and _NUMERIC_CHAT_RE.fullmatch(raw):
        return raw
    return raw


def _chat_path(value: Any) -> str:
    """Map a Telegram chat ID to the internal ``t.me/c`` path component."""

    raw = str(value or "").strip()
    if raw.startswith("-100") and raw[4:].isdigit():
        return raw[4:]
    if raw.startswith("-") and raw[1:].isdigit():
        return raw[1:]
    return raw if raw.isdigit() else ""


def telegram_message_link(
    *,
    identifier: Any = "",
    chat_id: Any = None,
    message_id: Any = None,
    fallback: str = "#",
) -> str:
    """Build a public Telegram message URL or return ``fallback``.

    A username link uses ``https://t.me/name/message``.  Numeric/non-username
    channels use the private ``https://t.me/c/internal_id/message`` form, with
    Telegram's ``-100`` prefix removed.  The function never emits a dangling
    ``#`` with a missing ID as a real URL.
    """

    message = str(message_id or "").strip()
    if not message.isdigit() or int(message) <= 0:
        return fallback
    normalized = normalize_channel_identifier(identifier)
    if normalized.startswith("@"):
        name = normalized[1:]
        if name and _USERNAME_RE.fullmatch(name) and not name.isdigit():
            return f"https://t.me/{name}/{message}"
    elif normalized and _USERNAME_RE.fullmatch(normalized) and not normalized.isdigit():
        # Discussion usernames are stored without @ by Telethon.
        return f"https://t.me/{normalized}/{message}"

    # Numeric identifiers are configuration hints, not sufficient to prove a
    # public link target.  Use the resolved Telegram chat_id; until collection
    # resolves it, fail closed with the caller's fallback.
    internal = _chat_path(chat_id)
    if internal:
        return f"https://t.me/c/{internal}/{message}"
    return fallback


__all__ = ["normalize_channel_identifier", "telegram_message_link"]
