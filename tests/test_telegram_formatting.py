from telethon import types

from app.telegram_formatting import render_telegram_html, serialize_entities


def test_entities_are_normalized_to_safe_json_fields():
    entities = serialize_entities(
        [
            types.MessageEntityBold(offset=0, length=4),
            types.MessageEntityTextUrl(offset=4, length=4, url="https://example.test"),
            types.MessageEntityPre(offset=8, length=3, language="python"),
        ]
    )

    assert entities == [
        {"type": "bold", "offset": 0, "length": 4},
        {"type": "text_url", "offset": 4, "length": 4, "url": "https://example.test"},
        {"type": "pre", "offset": 8, "length": 3, "language": "python"},
    ]


def test_renderer_handles_nested_entities_and_escapes_message_text():
    body = "<tag> is bold"
    rendered = render_telegram_html(
        body,
        [
            types.MessageEntityBold(offset=0, length=len(body)),
            types.MessageEntityItalic(offset=9, length=4),
        ],
    )

    assert rendered == "<strong>&lt;tag&gt; is </strong><strong><em>bold</em></strong>"


def test_renderer_uses_telegram_utf16_offsets_for_emoji():
    body = "A😀bold"
    # The emoji occupies two UTF-16 code units, so bold starts at offset 3.
    rendered = render_telegram_html(body, [types.MessageEntityBold(offset=3, length=4)])

    assert rendered == "A😀<strong>bold</strong>"


def test_renderer_preserves_links_code_quotes_and_spoilers_safely():
    body = "docs code hidden"
    rendered = render_telegram_html(
        body,
        [
            types.MessageEntityTextUrl(offset=0, length=4, url="https://example.test/?a=1&b=2"),
            types.MessageEntityCode(offset=5, length=4),
            types.MessageEntitySpoiler(offset=10, length=6),
        ],
    )

    assert '<a href="https://example.test/?a=1&amp;b=2"' in rendered
    assert "<code>code</code>" in rendered
    assert '<span class="telegram-spoiler">hidden</span>' in rendered


def test_renderer_rejects_script_urls_and_invalid_ranges():
    rendered = render_telegram_html(
        "unsafe",
        [
            {"type": "text_url", "offset": 0, "length": 6, "url": "javascript:alert(1)"},
            {"type": "bold", "offset": -3, "length": 4},
        ],
    )

    assert rendered == "unsafe"
    assert "javascript" not in rendered


def test_renderer_ignores_malformed_urls_without_failing():
    rendered = render_telegram_html(
        "broken",
        [{"type": "text_url", "offset": 0, "length": 6, "url": "http://["}],
    )

    assert rendered == "broken"
