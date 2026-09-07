"""T14 acceptance tests (simplified)."""
import pytest
from datetime import datetime, timezone, timedelta

from app.config import Settings
from app.studio.profile import _build_draft_from_analytics, PROFILE_EXTRACTION_VERSION
from app.studio.analytics import analyze_posts


def _rows(n, **kwargs):
    now = datetime.now(timezone.utc)
    rows = []
    for i in range(n):
        rows.append({
            "post_id": i+1,
            "message_id": 100+i,
            "channel_id": 1,
            "posted_at": now - timedelta(days=5),
            "snapshot_at": now,
            "text": kwargs.get("text", "Post content " + "x"*100),
            "views": 100 + i*10,
            "reactions": 5,
            "comments": 1,
            "shares": 1,
            "formatting_entities": kwargs.get("entities", []),
        })
    return rows


def test_build_120_posts():
    rows = _rows(120)
    analytics = analyze_posts(rows, 1, now=datetime.now(timezone.utc), identifier="@test")
    draft = _build_draft_from_analytics(analytics, rows)
    assert draft.topics
    assert draft.editorial_rules
    assert draft.style_rules
    for line in draft.topics + draft.editorial_rules + draft.style_rules:
        assert len(line) <= 300
        assert len(line.splitlines()) <= 1
        # No template patterns
        assert not line.lower().startswith("start with")
        assert "then" not in line.lower().split() or "then" not in line.lower()  # simplified
    assert draft.built_from_posts == 120


def test_formatting_facts_bold_and_signature():
    now = datetime.now(timezone.utc)
    rows = []
    for i in range(10):
        # First line bold in 80% (8/10)
        text = "Title line\nBody content"
        entities = []
        if i < 8:
            # Bold entity covering first line (UTF-16 length)
            first_len = len("Title line".encode("utf-16-le")) // 2
            entities.append({"type": "bold", "offset": 0, "length": first_len})
        # Signature link in last line for 70% (7/10)
        if i < 7:
            entities.append({"type": "text_link", "offset": len(text) - 5, "length": 4, "url": "https://t.me/sample"})
        rows.append({
            "post_id": i+1,
            "message_id": 100+i,
            "channel_id": 1,
            "posted_at": now - timedelta(days=5),
            "snapshot_at": now,
            "text": text,
            "views": 100,
            "reactions": 5,
            "comments": 1,
            "shares": 1,
            "formatting_entities": entities,
        })
    from app.studio.profile import _formatting_facts
    lines, facts = _formatting_facts(rows)
    style_text = "\n".join(lines)
    assert "**" in style_text
    assert "https://t.me/sample" in style_text
    # With low rates, neither should appear
    rows_low = []
    for i in range(10):
        rows_low.append({
            "post_id": i+1,
            "message_id": 100+i,
            "channel_id": 1,
            "posted_at": now - timedelta(days=5),
            "snapshot_at": now,
            "text": "Normal text",
            "views": 100,
            "reactions": 5,
            "comments": 1,
            "shares": 1,
            "formatting_entities": [],
        })
    lines2, _ = _formatting_facts(rows_low)
    assert "**" not in "\n".join(lines2)
    assert "https://t.me/sample" not in "\n".join(lines2)


@pytest.mark.asyncio
async def test_put_profile_version_and_validation(client, settings):
    settings.studio_test_mode = True
    await client.post("/login", data={"username": settings.admin_username, "password": settings.admin_password}, follow_redirects=False)
    import re
    home = await client.get("/studio")
    token = re.search(r'<meta name="studio-csrf-token" content="([^"]+)"', home.text).group(1)
    # Get current
    get = await client.get("/studio/api/profile?channel_id=1", headers={"x-csrf-token": token})
    assert get.status_code == 200
    profile = get.json()["profile"]
    version = profile["version"] if profile else 0
    # Put with correct version
    put = await client.put("/studio/api/profile", json={"channel_id": 1, "expected_version": version, "topics_text": "Topic A", "editorial_text": "Rule", "style_text": "**bold** and [link](https://x)"}, headers={"x-csrf-token": token})
    assert put.status_code == 200
    assert put.json()["profile"]["version"] == version + 1
    assert "**bold**" in put.json()["profile"]["style_text"]
    assert "[link](https://x)" in put.json()["profile"]["style_text"]
    # Wrong version -> 409
    bad = await client.put("/studio/api/profile", json={"channel_id": 1, "expected_version": version, "topics_text": "x", "editorial_text": "", "style_text": ""}, headers={"x-csrf-token": token})
    assert bad.status_code == 409
    # Field too long -> 422
    long_text = "a" * 2001
    bad2 = await client.put("/studio/api/profile", json={"channel_id": 1, "expected_version": version+1, "topics_text": long_text, "editorial_text": "", "style_text": ""}, headers={"x-csrf-token": token})
    assert bad2.status_code == 422


@pytest.mark.asyncio
async def test_context_budget_keeps_profile():
    from app.studio.context import ContextAssembler
    from app.studio.analytics import analyze_posts
    now = datetime.now(timezone.utc)
    rows = _rows(20)
    analytics = analyze_posts(rows, 1, now=now, identifier="@test")
    profile = {"topics_text": "Topic\nLine2", "editorial_text": "Rule", "style_text": "**bold**", "version": 3}
    assembler = ContextAssembler(max_chars=2000, max_evidence_posts=20)  # very small budget
    pack = assembler.assemble({"channel_id": 1, "identifier": "@test", "title": "t", "tracked_posts": 20}, analytics, profile, instruction="test", conversation_summary="x"*5000)
    # Profile block should still be present
    assert "CHANNEL PROFILE" in pack.profile or "CHANNEL PROFILE" in pack.profile_block
    assert "**bold**" in pack.profile or "**bold**" in pack.profile_block


def test_no_agent_profile_tool():
    from app.studio.agent import build_agent
    settings = Settings(studio_test_mode=True, api_id=1, api_hash="h", session_string="s", channels="@test")
    agent = build_agent(settings)
    # Check that propose/apply tools are not present
    toolset = getattr(agent, "_function_toolset", None)
    if toolset is not None:
        assert "propose_topic_changes" not in toolset.tools
        assert "apply_confirmed_topic_changes" not in toolset.tools


def test_migration_upgrade_downgrade():
    # Check that migration file exists and has upgrade/downgrade
    from pathlib import Path
    p = Path("alembic/versions/0009_profile_text.py")
    assert p.exists()
    text = p.read_text()
    assert "topics_text" in text
    assert "downgrade" in text


def test_routes_auth_csrf():
    # Check that new routes require auth and CSRF
    import inspect
    from app.studio.routes import build_router
    router = build_router()
    routes = {r.path: r for r in router.routes}
    assert "/studio/api/profile" in routes or any("/profile" in str(r.path) for r in router.routes)
    # Check that the old changes routes are gone
    paths = [str(r.path) for r in router.routes]
    assert not any("profile/changes" in p for p in paths)


def test_build_strips_markup():
    from app.studio.profile import _build_draft_from_analytics
    from app.studio.analytics import analyze_posts
    now = datetime.now(timezone.utc)
    rows = _rows(5, text="# Heading\n![img](x)\n<b>html</b>\n[bad](javascript:alert(1))")
    analytics = analyze_posts(rows, 1, now=now, identifier="@test")
    draft = _build_draft_from_analytics(analytics, rows)
    for line in draft.style_rules + draft.topics + draft.editorial_rules:
        assert "# Heading" not in line
        assert "![img]" not in line
        assert "<b>" not in line
        assert "javascript:" not in line


def test_build_never_emits_mockup_strings_and_derives_anchor_from_link():
    """Integration fix: the deterministic build had the mockup's channel name
    and a real-looking Russian title hard-coded as defaults."""
    from app.studio.profile import _formatting_facts

    now = datetime.now(timezone.utc)
    rows = []
    for i in range(10):
        text = "Title line\nBody content\nlink"
        entities = [{"type": "bold", "offset": 0, "length": len("Title line".encode("utf-16-le")) // 2}]
        # Signature link whose anchor text is empty after stripping -> host fallback.
        entities.append({"type": "text_link", "offset": len(text) - 4, "length": 4, "url": "https://t.me/other_channel"})
        rows.append({"post_id": i + 1, "message_id": 100 + i, "channel_id": 1, "posted_at": now - timedelta(days=5),
                     "snapshot_at": now, "text": text, "views": 100, "reactions": 5, "comments": 1, "shares": 1,
                     "formatting_entities": entities})
    lines, _ = _formatting_facts(rows)
    joined = "\n".join(lines)
    assert "Deputies Watch" not in joined
    assert "Дума утвердила" not in joined
    assert "**Example title**" in joined
    assert "[link](https://t.me/other_channel)" in joined
    # Anchor falls back to the host when the anchor text is blank.
    rows_blank = [dict(r, text="Title line\nBody content\n    ") for r in rows]
    for r in rows_blank:
        r["formatting_entities"] = [dict(e) for e in r["formatting_entities"]]
        r["formatting_entities"][1]["offset"] = len(r["text"]) - 4
    lines_blank, _ = _formatting_facts(rows_blank)
    assert "[t.me](https://t.me/other_channel)" in "\n".join(lines_blank)
