"""Markdown rendering for draft bodies - Telegram HTML subset.

Dialect from spec 1.1: **bold**, *italic*, ~~strike~~, `code`, [text](https://url), > quote,
single newlines as line breaks. No headings, images, HTML, tables. Links only http/https.
"""

from __future__ import annotations

import html
import re

# Only actual tags in the Telegram HTML subset are forbidden.  Angle-bracket
# prose such as ``a < b and c > d`` and ``use <your name>`` is ordinary text.
_REAL_TAG_PATTERN = re.compile(
    r"</?(?:a|b|i|u|s|em|strong|code|pre|p|br|div|span|img|script|style|blockquote|h[1-6]|ul|ol|li|table|tr|td|th)\b(?:\s+[A-Za-z_:][\w:.-]*\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+))*\s*/?>",
    re.IGNORECASE,
)


def _safe_url(url: str) -> str | None:
    url = url.strip()
    if not url:
        return None
    lowered = url.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        # Basic sanity: no spaces, no control chars
        if any(ord(c) < 0x20 for c in url):
            return None
        return url
    return None


def _strip_forbidden(text: str) -> str:
    """Remove unsupported markdown and real HTML tags for plain rendering."""
    # Remove images entirely
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    # Remove headings at line start (#, ## etc)
    text = re.sub(r"^\s*#{1,6}\s+", "", text, flags=re.MULTILINE)
    # Remove only actual tags; angle-bracket prose must survive verbatim.
    text = _REAL_TAG_PATTERN.sub("", text)
    return text


def _validate_body(body: str) -> None:
    """Raise DraftValidationError if body contains forbidden markdown per spec."""
    from .drafts import DraftValidationError

    # Check for headings: line starting with # + space
    for line in body.splitlines():
        if re.match(r"^\s*#{1,6}\s+", line):
            raise DraftValidationError("invalid_markdown", "Headings are not allowed in the draft body.", field="body")
        # Check for images: ![alt](url)
        if re.search(r"!\[", line):
            raise DraftValidationError("invalid_markdown", "Images are not allowed in the draft body.", field="body")
        # Check for real tags only; comparisons and placeholders are prose.
        if _REAL_TAG_PATTERN.search(line):
            raise DraftValidationError("invalid_markdown", "HTML is not allowed in the draft body.", field="body")


def render_markdown_html(body: str) -> str:
    """Render markdown body to Telegram HTML subset.

    Dialect: emphasis, strikethrough, backticks, link, blockquote, newline enabled;
    heading, image, html_inline, html_block, table disabled. Links only http/https.
    Output uses <b> <i> <s> <code> <a href> <blockquote>, escaped with html.escape.
    """
    if body is None:
        return ""
    text = str(body).replace("\r\n", "\n").replace("\r", "\n")
    # We process line by line, handling blockquote
    lines = text.split("\n")
    rendered_lines: list[str] = []
    for line in lines:
        # Handle blockquote: both ``>quote`` and ``> quote`` are accepted.
        is_quote = bool(re.match(r"^\s*>\s?", line))
        if is_quote:
            content = line.lstrip()[1:]
            if content.startswith(" "):
                content = content[1:]
            inner = _render_inline_html(content)
            rendered_lines.append(f"<blockquote>{inner}</blockquote>")
        else:
            rendered_lines.append(_render_inline_html(line))
    # This HTML is a clipboard payload. Bare newlines collapse to spaces when
    # pasted as HTML, so line breaks must be explicit <br> tags.
    return "<br>".join(rendered_lines)


def _render_inline_html(line: str) -> str:
    """Render inline markdown for a single line to Telegram HTML."""
    # Strip unsupported markdown first (render images as alt text only).
    line = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", line)
    # Remove heading markers if any slipped (should have been validated)
    line = re.sub(r"^\s*#{1,6}\s+", "", line)
    # Real tags are invalid input but may occur in legacy rows; remove the tag
    # delimiters while preserving their visible text in a direct render.
    line = _REAL_TAG_PATTERN.sub("", line)

    # Replace generated tags with slots, escape all user text exactly once,
    # then restore the slots.  Escaping the finished HTML would turn
    # ``&amp;`` into ``&amp;amp;`` inside formatted spans.
    slots: list[str] = []

    def slot(value: str) -> str:
        marker = f"\x00tg-html-{len(slots)}\x00"
        slots.append(value)
        return marker

    line = re.sub(
        r"`([^`]+)`",
        lambda m: slot(f"<code>{html.escape(m.group(1), quote=False)}</code>"),
        line,
    )

    # Handle links: [text](url) - only http/https
    def link_repl(m):
        label = m.group(1)
        url = m.group(2).strip()
        safe = _safe_url(url)
        if safe:
            escaped_url = html.escape(safe, quote=True)
            escaped_label = html.escape(label, quote=False)
            return slot(f'<a href="{escaped_url}">{escaped_label}</a>')
        else:
            return label

    line = re.sub(r"\[([^\]]+)\]\(((?:[^()\s]|\([^()\s]*\))+)\)", link_repl, line)

    # Handle bold: **bold**
    line = re.sub(r"\*\*([^*]+)\*\*", lambda m: slot(f"<b>{html.escape(m.group(1), quote=False)}</b>"), line)

    # Handle italic: *italic* - but not ** already handled
    def italic_repl(m):
        inner = m.group(1)
        if "**" in inner:
            return m.group(0)
        return slot(f"<i>{html.escape(inner, quote=False)}</i>")

    line = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", italic_repl, line)

    # Handle strikethrough: ~~strike~~
    line = re.sub(r"~~([^~]+)~~", lambda m: slot(f"<s>{html.escape(m.group(1), quote=False)}</s>"), line)

    escaped = html.escape(line, quote=False)
    for index, value in enumerate(slots):
        escaped = escaped.replace(f"\x00tg-html-{index}\x00", value)
    return escaped


def render_markdown_plain(body: str) -> str:
    """Strip markup and return plain text; for links, append (url) when anchor != url."""
    if body is None:
        return ""
    text = str(body).replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    plain_lines: list[str] = []
    for line in lines:
        # Handle blockquote: strip either ``>`` or ``> ``.
        if re.match(r"^\s*>\s?", line):
            line = line.lstrip()[1:]
            if line.startswith(" "):
                line = line[1:]
        # Handle code: `code` -> code
        line = re.sub(r"`([^`]+)`", r"\1", line)
        # Handle images: ![alt](url) -> alt
        line = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", line)
        # Handle headings: # heading -> heading
        line = re.sub(r"^\s*#{1,6}\s+", "", line)
        # Handle real HTML tags only; angle-bracket prose is preserved.
        line = _REAL_TAG_PATTERN.sub("", line)
        # Handle bold/italic/strike: **bold** -> bold, *italic* -> italic, ~~strike~~ -> strike
        line = re.sub(r"\*\*([^*]+)\*\*", r"\1", line)
        line = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"\1", line)
        line = re.sub(r"~~([^~]+)~~", r"\1", line)
        # Handle links: [text](url) -> text (url) if text != url
        def plain_link(m):
            label = m.group(1)
            url = m.group(2).strip()
            safe = _safe_url(url)
            if safe:
                if label.strip() == safe.strip():
                    return label
                else:
                    return f"{label} ({safe})"
            else:
                return label

        line = re.sub(r"\[([^\]]+)\]\(((?:[^()\s]|\([^()\s]*\))+)\)", plain_link, line)
        plain_lines.append(line)
    return "\n".join(plain_lines)


def validate_markdown_body(body: str) -> None:
    """Validate that body does not contain forbidden constructs; raise if so."""
    _validate_body(body)


__all__ = ["render_markdown_html", "render_markdown_plain", "validate_markdown_body"]
