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


import pytest as _pytest


@_pytest.mark.asyncio
async def test_restore_identical_version_does_not_mint_a_new_version():
    from app.studio.repository import MemoryStudioRepository

    repo = MemoryStudioRepository()
    conversation = await repo.create_conversation(channel_id=1)
    draft = await repo.create_draft(conversation_id=conversation["id"], channel_id=1, payload={"body": "v1 body", "creative": True})
    v2 = await repo.update_draft(draft_id=draft["id"], payload={"body": "v2 body"}, expected_revision=draft["revision"], origin="user_edit")
    assert v2["current_version"] == 2
    restored = await repo.restore_draft_version(draft_id=draft["id"], version=1, expected_revision=v2["revision"])
    assert restored["body"] == "v1 body" and restored["current_version"] == 3
    # Restoring v1 again (identical to the current text) returns the current row unchanged.
    again = await repo.restore_draft_version(draft_id=draft["id"], version=1, expected_revision=restored["revision"])
    assert again["current_version"] == 3
    assert len(await repo.list_draft_versions(draft_id=draft["id"])) == 3
