"""Copy fidelity and no source lists in the post body."""
from app.studio.agent import _clean_publication_text, _strip_trailing_source_list
from app.studio.markdown import render_markdown_html


def test_trailing_source_list_is_removed_but_inline_links_stay():
    body = (
        "**Заголовок**\n\n"
        "Совет утвердил бюджет, [документ](https://zsro.ru/doc) опубликован.\n\n"
        "Источники:\n- https://zsro.ru/doc\n- [Коммерсант](https://kommersant.ru/x)"
    )
    cleaned, removed = _strip_trailing_source_list(body)
    assert removed is True
    assert cleaned.endswith("опубликован.")
    assert "[документ](https://zsro.ru/doc)" in cleaned
    assert "Источники" not in cleaned

    english = "Text.\n\nSources: https://a.example, https://b.example"
    cleaned2, removed2 = _strip_trailing_source_list(english)
    assert (cleaned2, removed2) == ("Text.", True)

    # A trailing paragraph that merely mentions sources without links is prose and stays.
    prose = "Text.\n\nSources close to the committee say the vote is postponed."
    assert _strip_trailing_source_list(prose) == (prose, False)
    # A header without links underneath is not a list either.
    assert _strip_trailing_source_list("Text.\n\nLinks:") == ("Text.\n\nLinks:", False)


def test_clean_publication_text_reports_removed_source_list():
    cleaned, removed = _clean_publication_text("Post body.\n\n**Sources**\nhttps://x.example/1\nhttps://x.example/2")
    assert cleaned == "Post body." and removed is True
    unchanged, removed_none = _clean_publication_text("Post body with an inline [link](https://x.example).")
    assert removed_none is False and unchanged.startswith("Post body with an inline")


def test_clipboard_html_uses_br_line_breaks():
    html = render_markdown_html("**Title**\nSecond line\n> quote")
    assert html == "<b>Title</b><br>Second line<br><blockquote>quote</blockquote>"
    assert "\n" not in html


def test_links_with_balanced_parentheses_render_whole():
    from app.studio.markdown import render_markdown_plain

    body = "See [w](https://en.wikipedia.org/wiki/Foo_(bar)) and [j](javascript:alert(1)) end"
    html = render_markdown_html(body)
    assert '<a href="https://en.wikipedia.org/wiki/Foo_(bar)">w</a>' in html
    assert "javascript" not in html.lower() and "j end" in html
    assert render_markdown_plain(body) == "See w (https://en.wikipedia.org/wiki/Foo_(bar)) and j end"
