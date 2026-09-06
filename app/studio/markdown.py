"""Markdown rendering for draft bodies - Telegram HTML subset.

Dialect from spec 1.1: **bold**, *italic*, ~~strike~~, `code`, [text](https://url), > quote,
single newlines as line breaks. No headings, images, HTML, tables. Links only http/https.
"""

from __future__ import annotations

import html
import re

# Try to use markdown-it-py if available, but implement fallback regex for precise dialect control.
try:
    from markdown_it import MarkdownIt  # type: ignore

    _HAS_MD = True
except ImportError:
    _HAS_MD = False


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
    """Remove headings, images, HTML for plain rendering - but for html we convert."""
    # Remove images entirely
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    # Remove headings at line start (#, ## etc)
    text = re.sub(r"^\s*#{1,6}\s+", "", text, flags=re.MULTILINE)
    # Remove HTML tags
    text = re.sub(r"<[a-zA-Z/][^>]*>", "",text)
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
        # Check for HTML tags
        if re.search(r"<[a-zA-Z][^>]*>", line):
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
    # First validate - but for rendering we just strip forbidden
    # Use regex approach for deterministic Telegram HTML
    # Escape and wrap incrementally
    # We process line by line, handling blockquote
    lines = text.split("\n")
    rendered_lines: list[str] = []
    for line in lines:
        # Handle blockquote: line starting with > 
        is_quote = line.lstrip().startswith("> ")
        if is_quote:
            content = line.lstrip()[2:]
            inner = _render_inline_html(content)
            rendered_lines.append(f"<blockquote>{inner}</blockquote>")
        else:
            rendered_lines.append(_render_inline_html(line))
    # Join with newline - Telegram preserves \n; for HTML copy we use \n as well
    # Spec says single newlines as line breaks; we keep \n (Telegram will treat as line break)
    # For HTML clipboard, we keep as \n inside <b> etc - but also could use <br>? Keep \n.
    return "\n".join(rendered_lines)


def _render_inline_html(line: str) -> str:
    """Render inline markdown for a single line to Telegram HTML."""
    # Strip images first (render as alt text only)
    line = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", line)
    # Remove heading markers if any slipped (should have been validated)
    line = re.sub(r"^\s*#{1,6}\s+", "", line)
    # Remove HTML tags (strip)
    line = re.sub(r"<[a-zA-Z/][^>]*>", "",line)

    # Handle code spans: `code` -> <code>code</code> (escape inside)
    line = re.sub(r"`([^`]+)`", lambda m: f"<code>{html.escape(m.group(1), quote=False)}</code>", line)

    # Handle links: [text](url) - only http/https
    def link_repl(m):
        label = m.group(1)
        url = m.group(2).strip()
        safe = _safe_url(url)
        if safe:
            escaped_url = html.escape(safe, quote=True)
            escaped_label = html.escape(label, quote=False)
            return f'<a href="{escaped_url}">{escaped_label}</a>'
        else:
            return label

    line = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link_repl, line)

    # Handle bold: **bold**
    line = re.sub(r"\*\*([^*]+)\*\*", lambda m: f"<b>{html.escape(m.group(1), quote=False)}</b>", line)

    # Handle italic: *italic* - but not ** already handled
    def italic_repl(m):
        inner = m.group(1)
        if "**" in inner:
            return m.group(0)
        return f"<i>{html.escape(inner, quote=False)}</i>"

    line = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", italic_repl, line)

    # Handle strikethrough: ~~strike~~
    line = re.sub(r"~~([^~]+)~~", lambda m: f"<s>{html.escape(m.group(1), quote=False)}</s>", line)

    # Escape everything except the tags this renderer itself generated. Any
    # other "<…>" span is prose (for example "a < b and c > d") and must be
    # escaped, otherwise it would reach the rich clipboard as markup.
    parts = re.split(r"(</?(?:b|i|s|code|a)\b[^>]*>)", line)
    escaped_parts: list[str] = []
    for part in parts:
        if re.fullmatch(r"</?(?:b|i|s|code|a)\b[^>]*>", part):
            escaped_parts.append(part)
        else:
            escaped_parts.append(html.escape(part, quote=False))
    return "".join(escaped_parts)


def render_markdown_plain(body: str) -> str:
    """Strip markup and return plain text; for links, append (url) when anchor != url."""
    if body is None:
        return ""
    text = str(body).replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    plain_lines: list[str] = []
    for line in lines:
        # Handle blockquote: strip > 
        if line.lstrip().startswith("> "):
            line = line.lstrip()[2:]
        # Handle code: `code` -> code
        line = re.sub(r"`([^`]+)`", r"\1", line)
        # Handle images: ![alt](url) -> alt
        line = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", line)
        # Handle headings: # heading -> heading
        line = re.sub(r"^\s*#{1,6}\s+", "", line)
        # Handle HTML: strip
        line = re.sub(r"<[a-zA-Z/][^>]*>", "",line)
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

        line = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", plain_link, line)
        plain_lines.append(line)
    return "\n".join(plain_lines)


def validate_markdown_body(body: str) -> None:
    """Validate that body does not contain forbidden constructs; raise if so."""
    _validate_body(body)


__all__ = ["render_markdown_html", "render_markdown_plain", "validate_markdown_body"]
