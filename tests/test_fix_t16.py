"""T16 acceptance tests: Markdown drafts end-to-end."""

import pytest
from app.studio.markdown import render_markdown_html, render_markdown_plain
from app.studio.drafts import DraftValidationError, validate_draft_input


def test_markdown_html_renders_bold_and_link():
    body = "**Title**\nText with a [source](https://example.com)"
    html = render_markdown_html(body)
    assert "<b>Title</b>" in html
    assert '<a href="https://example.com"' in html
    assert "source" in html
    plain = render_markdown_plain(body)
    assert "Title" in plain
    assert "source (https://example.com)" in plain


def test_markdown_rejects_heading_and_script():
    for bad in ["# Heading", "## Another", "<script>alert(1)</script>", "<b>html</b>", "![img](https://x)"]:
        try:
            validate_draft_input({"body": bad, "creative": True}, known_source_ids=set(), require_sources=False)
            # Should have raised
            assert False, f"should reject {bad!r}"
        except DraftValidationError as e:
            assert e.code == "invalid_markdown"
            assert e.field == "body"


def test_markdown_javascript_link_renders_as_text():
    body = "Check [x](javascript:alert(1)) and [y](https://example.com)"
    html = render_markdown_html(body)
    assert "javascript" not in html.lower()
    assert "x" in html
    assert 'href="https://example.com"' in html
    plain = render_markdown_plain(body)
    assert "x" in plain
    assert "y (https://example.com)" in plain
    assert "javascript" not in plain.lower()


def test_plain_counts_over_limit():
    # **bold** is 8 chars markdown but 4 plain, so over_limit should count plain
    body = "**a**" * 2000  # markdown length 8000, plain length 2000
    # Plain would be "a"*2000 = 2000, not over
    plain = render_markdown_plain(body)
    assert len(plain) == 2000
    # Now make plain over limit: 4097 plain chars
    body2 = "x" * 4097
    payload = validate_draft_input({"body": body2, "creative": True}, known_source_ids=set(), require_sources=False)
    assert payload.over_limit is True
    assert payload.character_count == 4097
    # Markdown that renders to over via plain
    body3 = "**" + "x" * 4097 + "**"
    payload3 = validate_draft_input({"body": body3, "creative": True}, known_source_ids=set(), require_sources=False)
    # plain is 4097 x's, so over
    assert payload3.over_limit is True
    assert payload3.character_count == 4097


@pytest.mark.asyncio
async def test_copy_endpoint_returns_both(client, app, settings):
    settings.studio_enabled = True
    settings.studio_test_mode = True
    await client.post("/login", data={"username": settings.admin_username, "password": settings.admin_password}, follow_redirects=False)
    import re, uuid
    home = await client.get("/studio")
    token = re.search(r'<meta name="studio-csrf-token" content="([^"]+)"', home.text).group(1)
    conv = await client.post("/studio/api/conversations", json={}, headers={"x-csrf-token": token})
    assert conv.status_code == 200
    conv_id = conv.json()["conversation"]["id"]
    bootstrap = await client.get("/studio/api/bootstrap", headers={"x-csrf-token": token})
    ch_id = bootstrap.json()["selected_channel_id"] or 1
    body = "**Title**\nText with [link](https://example.com)"
    # Create draft via repository directly (bypassing agent)
    repo = app.state.studio_repository
    draft = await repo.create_draft(conversation_id=uuid.UUID(conv_id), channel_id=int(ch_id), payload={"body": body, "working_title": "Test", "creative": True, "source_ids": [], "claim_support": []}, origin="generated", instruction="test")
    draft_id = draft["id"]
    resp = await client.post(f"/studio/api/drafts/{draft_id}/copied", headers={"x-csrf-token": token})
    assert resp.status_code == 200
    data = resp.json()
    assert "copied_text" in data
    assert "copied_html" in data
    assert "<b>Title</b>" in data["copied_html"]
    assert '<a href="https://example.com"' in data["copied_html"]
    assert "Title" in data["copied_text"]
    assert "link (https://example.com)" in data["copied_text"]


def test_strike_and_code_and_quote():
    body = "This is ~~strike~~ and `code`\n> quote"
    html = render_markdown_html(body)
    assert "<s>strike</s>" in html
    assert "<code>code</code>" in html
    assert "<blockquote>" in html
    plain = render_markdown_plain(body)
    assert "strike" in plain
    assert "code" in plain
    assert "quote" in plain
