"""Normalize and safely render Telegram message formatting entities.

Telethon exposes message formatting separately from ``Message.message``.  The
offsets are UTF-16 code-unit offsets, so this module uses Telethon's surrogate
helpers before slicing.  Entity values are deliberately reduced to a small,
provider-independent JSON shape; the web layer then generates HTML itself and
escapes all message text and link attributes.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

from telethon.utils import add_surrogate, del_surrogate


_TYPE_ALIASES = {
    "bold": "bold",
    "italic": "italic",
    "underline": "underline",
    "strike": "strike",
    "strikethrough": "strike",
    "code": "code",
    "pre": "pre",
    "blockquote": "blockquote",
    "spoiler": "spoiler",
    "texturl": "text_url",
    "url": "url",
    "email": "email",
    "mention": "mention",
    "mentionname": "mention_name",
    "hashtag": "hashtag",
    "cashtag": "cashtag",
    "botcommand": "bot_command",
    "phone": "phone",
    "customemoji": "custom_emoji",
    "bankcard": "bank_card",
}
_LANGUAGE_RE = re.compile(r"^[A-Za-z0-9_+.#-]{1,32}$")
_SAFE_EMAIL_RE = re.compile(r"^[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+$")


def _canonical_type(value: Any) -> str:
    raw = str(value or "").strip()
    raw = raw.rsplit(".", 1)[-1]
    raw = raw.removeprefix("MessageEntity")
    compact = re.sub(r"[^a-z0-9]", "", raw.lower())
    return _TYPE_ALIASES.get(compact, compact or "unknown")


def _read_field(entity: Any, name: str, default: Any = None) -> Any:
    if isinstance(entity, Mapping):
        if name in entity:
            return entity[name]
        # Telethon's serialized objects sometimes use a leading underscore
        # for the constructor name but keep regular fields unchanged.
        return entity.get(name.lstrip("_"), default)
    return getattr(entity, name, default)


def _integer(value: Any) -> int | None:
    try:
        if isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def serialize_entities(entities: Iterable[Any] | None) -> list[dict[str, Any]]:
    """Return the bounded, JSON-safe subset of Telegram entity metadata."""

    result: list[dict[str, Any]] = []
    for entity in entities or ():
        type_name = _read_field(entity, "type") or _read_field(entity, "_")
        if not type_name and entity is not None:
            type_name = entity.__class__.__name__
        offset = _integer(_read_field(entity, "offset"))
        length = _integer(_read_field(entity, "length"))
        if offset is None or length is None or offset < 0 or length <= 0:
            continue
        item: dict[str, Any] = {
            "type": _canonical_type(type_name),
            "offset": offset,
            "length": length,
        }
        for field in ("url", "language"):
            value = _read_field(entity, field)
            if value not in (None, ""):
                item[field] = str(value)
        for field in ("collapsed", "document_id", "user_id"):
            value = _read_field(entity, field)
            if value is None:
                continue
            numeric = _integer(value) if field != "collapsed" else value
            if field == "collapsed":
                if isinstance(value, bool):
                    item[field] = value
            elif numeric is not None:
                item[field] = numeric
        result.append(item)
    return result


def normalize_entities(value: Any) -> list[dict[str, Any]]:
    """Load entity metadata from SQLite JSON, PostgreSQL JSONB, or Telethon."""

    if value in (None, "", b""):
        return []
    if isinstance(value, (str, bytes, bytearray)):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
    if isinstance(value, Mapping):
        value = [value]
    if not isinstance(value, Iterable):
        return []
    return serialize_entities(value)


def _safe_url(url: Any) -> str | None:
    """Allow only links that a browser can open without script/file payloads."""

    candidate = str(url or "").strip()
    if not candidate or any(ord(char) < 0x20 for char in candidate):
        return None
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if scheme in {"http", "https"} and parsed.netloc:
        return candidate
    if scheme == "mailto" and parsed.path and _SAFE_EMAIL_RE.match(parsed.path):
        return candidate
    if scheme == "tg" and (parsed.netloc or parsed.path):
        return candidate
    return None


def _language_class(language: Any) -> str:
    value = str(language or "").strip()
    if _LANGUAGE_RE.fullmatch(value):
        return f' class="language-{html.escape(value, quote=True)}"'
    return ""


def _wrapper(entity: Mapping[str, Any], source: str, start: int, end: int) -> tuple[str, str]:
    """Return an HTML open/close pair for one normalized entity."""

    kind = _canonical_type(entity.get("type"))
    if kind == "bold":
        return "<strong>", "</strong>"
    if kind == "italic":
        return "<em>", "</em>"
    if kind == "underline":
        return "<u>", "</u>"
    if kind == "strike":
        return "<s>", "</s>"
    if kind == "code":
        return "<code>", "</code>"
    if kind == "pre":
        return f"<pre><code{_language_class(entity.get('language'))}>", "</code></pre>"
    if kind == "blockquote":
        return "<blockquote>", "</blockquote>"
    if kind == "spoiler":
        return '<span class="telegram-spoiler">', "</span>"
    if kind in {"text_url", "url", "email"}:
        visible = del_surrogate(source[start:end])
        target = entity.get("url") if kind == "text_url" else visible
        if kind == "email" and target and "@" in str(target) and not str(target).lower().startswith("mailto:"):
            target = f"mailto:{target}"
        safe_target = _safe_url(target)
        if safe_target:
            escaped = html.escape(safe_target, quote=True)
            return (
                f'<a href="{escaped}" target="_blank" rel="noopener noreferrer">',
                "</a>",
            )
    return "", ""


def render_telegram_html(text: Any, entities: Any = None) -> str:
    """Render escaped Telegram text with a safe subset of rich formatting."""

    plain = str(text or "")
    if not plain:
        return ""
    source = add_surrogate(plain)
    source_length = len(source)
    normalized = normalize_entities(entities)
    intervals: list[tuple[int, int, int, Mapping[str, Any]]] = []
    for index, entity in enumerate(normalized):
        offset = _integer(entity.get("offset"))
        length = _integer(entity.get("length"))
        if offset is None or length is None or length <= 0:
            continue
        if offset < 0 or offset >= source_length:
            continue
        start = offset
        end = min(offset + length, source_length)
        if end > start:
            intervals.append((start, end, index, entity))
    if not intervals:
        return html.escape(plain, quote=False)

    boundaries = {0, source_length}
    for start, end, _, _ in intervals:
        boundaries.update((start, end))
    ordered_boundaries = sorted(boundaries)
    rendered: list[str] = []
    for left, right in zip(ordered_boundaries, ordered_boundaries[1:]):
        if right <= left:
            continue
        active = [item for item in intervals if item[0] <= left and item[1] >= right]
        active.sort(key=lambda item: (item[0], -item[1], item[2]))
        chunk = html.escape(del_surrogate(source[left:right]), quote=False)
        openings: list[str] = []
        closes: list[str] = []
        for entity_start, entity_end, _, entity in active:
            opening, closing = _wrapper(entity, source, entity_start, entity_end)
            if opening:
                openings.append(opening)
                closes.append(closing)
        if openings:
            chunk = "".join(openings) + chunk + "".join(reversed(closes))
        rendered.append(chunk)
    return "".join(rendered)


__all__ = ["normalize_entities", "render_telegram_html", "serialize_entities"]
