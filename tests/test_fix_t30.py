from __future__ import annotations

import pytest

from app.studio.drafts import validate_draft_input
from app.studio.markdown import render_markdown_html, render_markdown_plain, validate_markdown_body


@pytest.mark.parametrize(
    "body",
    [
        "if a<b and c>d then",
        "use <your name> here",
        ">quote",
        "> quote",
    ],
)
def test_angle_bracket_prose_and_both_blockquote_markers_round_trip(body: str):
    validate_markdown_body(body)
    expected = body.lstrip()[1:].removeprefix(" ") if body.lstrip().startswith(">") else body
    assert render_markdown_plain(body) == expected


def test_real_html_tags_remain_rejected():
    with pytest.raises(ValueError, match="HTML is not allowed"):
        validate_markdown_body("<b>x</b>")


def test_html_escapes_formatted_text_exactly_once():
    assert render_markdown_html("**Tom & Jerry**") == "<b>Tom &amp; Jerry</b>"
    assert render_markdown_html(">quote") == "<blockquote>quote</blockquote>"


def test_telegram_limit_uses_utf16_code_units():
    payload = validate_draft_input({"body": "a" * 4090 + "🙂" * 20, "creative": True}, require_sources=False)
    assert payload.character_count == 4130
    assert payload.over_limit is True
