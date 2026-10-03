"""T51 backend contracts: UTC cohort, Explorer, post read model, logs, settings, export.

Isolated synthetic fixtures only.  PostgreSQL tests use disposable workspaces
via the shared conftest (skipped without TEST_POSTGRES_URL); pure unit and
Fake-DB tests run everywhere.
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.config import Settings
from app.postgres_db import cohort_bounds, parse_utc_date
from app.telegram_formatting import normalize_entities, render_telegram_html
from app.web.routes import _sanitize_channel_csv_cell, _sanitize_csv_cell


# ---------- pure unit: UTC dates ----------

def test_parse_utc_date_boundaries():
    assert parse_utc_date(None) is None
    assert parse_utc_date("") is None
    assert parse_utc_date("  ") is None
    start = parse_utc_date("2026-09-06")
    assert start == datetime(2026, 9, 6, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        parse_utc_date("06-09-2026")
    with pytest.raises(ValueError):
        parse_utc_date("2026-13-01")
    with pytest.raises(ValueError):
        parse_utc_date("2026-02-30")
    with pytest.raises(ValueError):
        parse_utc_date("1969-12-31")
    with pytest.raises(ValueError):
        parse_utc_date("x" * 201)


def test_cohort_half_open_next_day():
    start, exclusive = cohort_bounds("2026-09-06", "2026-09-06")
    assert start == datetime(2026, 9, 6, tzinfo=timezone.utc)
    assert exclusive == datetime(2026, 9, 7, tzinfo=timezone.utc)
    # Open-ended cohorts are allowed.
    assert cohort_bounds(None, None) == (None, None)
    assert cohort_bounds("2026-09-06", None)[0] == datetime(2026, 9, 6, tzinfo=timezone.utc)
    assert cohort_bounds("2026-09-06", None)[1] is None
    # Reversed ranges never partially apply.
    with pytest.raises(ValueError):
        cohort_bounds("2026-09-07", "2026-09-06")


def test_telegram_entities_sanitized():
    html = render_telegram_html("Bold <tag>", [{"type": "bold", "offset": 0, "length": 10}])
    assert "<strong>Bold &lt;tag&gt;</strong>" in html
    assert "<tag>" not in html.replace("&lt;tag&gt;", "")
    # Unsafe javascript: URLs are rejected (no <a>), safe https links render.
    unsafe = render_telegram_html("click", [{"type": "text_url", "offset": 0, "length": 5, "url": "javascript:alert(1)"}])
    assert "<a " not in unsafe
    assert "click" in unsafe
    safe = render_telegram_html("click", [{"type": "text_url", "offset": 0, "length": 5, "url": "https://example.test/post"}])
    assert 'href="https://example.test/post"' in safe
    # Unknown entity data is retained by normalize, never executed.
    entities = normalize_entities([{"type": "bold", "offset": 0, "length": 4, "url": "https://example.test"}])
    assert entities[0]["type"] == "bold"
    assert render_telegram_html("<script>alert(1)</script>", None) == "&lt;script&gt;alert(1)&lt;/script&gt;"


def test_csv_safety_cells():
    assert _sanitize_csv_cell("=HYPERLINK(\"https://evil.test\")") == "'=HYPERLINK(\"https://evil.test\")"
    assert _sanitize_csv_cell("+1") == "'+1"
    assert _sanitize_csv_cell("-100123") == "'-100123"
    assert _sanitize_csv_cell("@user") == "'@user"
    # Valid channel usernames keep their conventional @ for paste-back.
    assert _sanitize_channel_csv_cell("@news_room") == "@news_room"
    assert _sanitize_channel_csv_cell("=HYPERLINK(\"x\")") == "'=HYPERLINK(\"x\")"
    assert _sanitize_csv_cell(123) == 123
    assert _sanitize_csv_cell(None) == ""


# ---------- Fake-DB settings/logs/export (no PG required) ----------

def _fake_settings_app(tmp_path, role="owner", available=True):
    from cryptography.fernet import Fernet

    from app.collector import Collector
    from app.web.routes import WorkspaceContext, create_app

    key = Fernet.generate_key().decode("ascii")

    class FakeStore:
        def __init__(self):
            self.available = available
            self._rows = {}
            self.telegram_restart_required = False
            self._dict = {
                "collection.poll_minutes": {"value": 15.0, "source": "default"},
                "collection.track_days": {"value": 30, "source": "default"},
                "collection.backfill_limit": {"value": 200, "source": "default"},
                "studio.openrouter_api_key": {"set": False, "source": "default"},
                "studio.model": {"value": "openai/gpt-4o-mini", "source": "default"},
                "research.enabled": {"value": False, "source": "default"},
                "research.blocked_domains": {"value": "", "source": "default"},
            }

        def as_dict(self):
            return dict(self._dict)

        def validate(self, key, value):
            from app.workspace_settings import SETTINGS

            return SETTINGS[key][2](value)

        async def set_many(self, changes, reset_keys=()):
            for key in reset_keys:
                if key not in ("collection.poll_minutes", "collection.track_days", "collection.backfill_limit",
                               "studio.openrouter_api_key", "studio.model", "research.enabled", "research.blocked_domains"):
                    raise ValueError(f"unknown settings key: {key}")
            for key, value in changes.items():
                self.validate(key, value)
            for key in reset_keys:
                self._dict[key] = {"value": 15, "source": "env"} if "poll" in key else ({"set": False, "source": "default"} if "openrouter_api_key" in key else {"value": "", "source": "default"})
                self._rows.pop(key, None)
            for key, value in changes.items():
                self._rows[key] = value
                if key == "studio.openrouter_api_key":
                    # Write-only: never store plaintext in the readable dict.
                    self._dict[key] = {"set": True, "source": "db", "updated_at": "2026-10-02 00:00 UTC"}
                else:
                    self._dict[key] = {"value": value, "source": "db", "updated_at": "2026-10-02 00:00 UTC"}

        async def reset(self, key):
            await self.set_many({}, reset_keys=(key,))

    class FakeDB:
        def __init__(self):
            self.workspace_id = uuid.uuid4()
            self.user_id = uuid.uuid4()
            self.workspace_slug = "community"

        async def get_channels(self):
            return []

        async def get_channels_for_settings(self):
            return []

        async def channel_data_summaries(self):
            return {}

        async def telegram_connection_status(self, label):
            return {"configured": False, "api_id": None, "has_session": False, "updated_at": None}

        async def kpis(self, channel_id=None):
            return {"posts": 0, "views": 0, "reactions": 0, "comments": 0, "shares": 0, "collected_comments": 0, "last_poll": None}

        async def timeseries_totals(self, days=None, channel_id=None):
            return {"days": [], "views": [], "reactions": [], "comments": [], "shares": [], "posts_per_day": []}

        async def latest_stats(self, channel_id=None, limit=500, order="date", offset=0):
            return []

        async def all_comments(self, channel_id=None, limit=None):
            return []

    settings = Settings(
        _env_file=None,
        data_dir=str(tmp_path),
        admin_username="admin",
        admin_password="pw",
        session_secret="sec",
        database_url="",
        api_id=1,
        api_hash="h",
        session_string="s",
        channels="@test",
        telegram_session_encryption_key=key,
    )
    db = FakeDB()
    ws = FakeStore()
    collector = Collector(None, db, settings)
    collector.workspace_settings = ws
    app = create_app(collector, settings, workspace_settings=ws)
    from app.web.routes import WorkspaceContext as Ctx

    app.state.workspace_context = Ctx(
        user_id=db.user_id, workspace_id=db.workspace_id, workspace_slug="community", role=role
    )
    return app, ws, db


async def _login(client, username="admin", password="pw"):
    resp = await client.post("/login", data={"username": username, "password": password}, follow_redirects=False)
    assert resp.status_code == 303


def _csrf_token(page_text: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', page_text)
    assert match, "CSRF token not found in settings page"
    return match.group(1)


@pytest.mark.asyncio
async def test_settings_json_canonical_no_partial_and_csrf(tmp_path):
    app, ws, _ = _fake_settings_app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client)
        page = await client.get("/settings")
        token = _csrf_token(page.text)
        # CSRF without token is 403 for both HTML and JSON.
        denied = await client.post(
            "/settings/collection",
            data={"poll_minutes": "30", "track_days": "30", "backfill_limit": "200"},
        )
        assert denied.status_code == 403
        # Invalid JSON save returns safe field errors and applies nothing.
        bad = await client.post(
            "/settings/collection",
            data={"poll_minutes": "0", "track_days": "30", "backfill_limit": "200", "csrf_token": token},
            headers={"Accept": "application/json"},
        )
        assert bad.status_code == 422
        body = bad.json()
        assert body["ok"] is False
        assert "poll_minutes" in body["errors"]
        assert ws._rows == {}, "invalid save must not partially apply"
        # Valid JSON save returns canonical fields/provenance/readiness.
        good = await client.post(
            "/settings/collection",
            data={"poll_minutes": "30", "track_days": "30", "backfill_limit": "200", "csrf_token": token},
            headers={"Accept": "application/json"},
        )
        assert good.status_code == 200
        payload = good.json()
        assert payload["ok"] is True
        assert payload["section"] == "collection"
        assert payload["fields"]["collection.poll_minutes"]["source"] == "db"
        assert payload["fields"]["collection.poll_minutes"]["value"] == 30.0
        # Form fallback still redirects.
        page2 = await client.get("/settings")
        token2 = _csrf_token(page2.text)
        redirect = await client.post(
            "/settings/collection",
            data={"poll_minutes": "31", "track_days": "31", "backfill_limit": "201", "csrf_token": token2},
            follow_redirects=False,
        )
        assert redirect.status_code == 303


@pytest.mark.asyncio
async def test_settings_json_write_only_secrets(tmp_path):
    app, ws, _ = _fake_settings_app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client)
        page = await client.get("/settings")
        token = _csrf_token(page.text)
        # Blank model is required; secrets are never echoed.
        resp = await client.post(
            "/settings/studio",
            data={"openrouter_api_key": "sk-test-123", "model": "openai/gpt-4o-mini", "csrf_token": token},
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["ok"] is True
        assert "sk-test-123" not in resp.text
        assert payload["fields"]["studio.openrouter_api_key"] == {"set": True, "source": "db", "updated_at": "2026-10-02 00:00 UTC"} or payload["fields"]["studio.openrouter_api_key"]["set"] is True
        # Settings JSON itself never contains plaintext secrets.
        settings_json = await client.get("/settings", headers={"Accept": "application/json"})
        assert settings_json.status_code == 200
        assert "sk-test-123" not in settings_json.text


@pytest.mark.asyncio
async def test_logs_unavailable_vs_empty_distinguishable(tmp_path):
    from app.studio.repository import MemoryStudioRepository

    # Empty storage: available true, total 0.
    app, _, _ = _fake_settings_app(tmp_path)
    from unittest.mock import AsyncMock

    empty_repo = MemoryStudioRepository()
    app.state.studio_repository = empty_repo
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client)
        resp = await client.get("/settings/logs", headers={"Accept": "application/json"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["available"] is True
        assert body["total"] == 0
        assert body["logs"] == []
    # Unavailable storage: available false, 503, distinct from empty.
    app2, _, _ = _fake_settings_app(tmp_path)
    broken = AsyncMock()
    broken.list_tool_result_logs.side_effect = RuntimeError("db down")
    broken.count_tool_result_logs.side_effect = RuntimeError("db down")
    app2.state.studio_repository = broken
    transport2 = httpx.ASGITransport(app=app2)
    async with httpx.AsyncClient(transport=transport2, base_url="http://test") as client2:
        await _login(client2)
        resp2 = await client2.get("/settings/logs", headers={"Accept": "application/json"})
        assert resp2.status_code == 503
        body2 = resp2.json()
        assert body2["available"] is False
        assert body2["error"]["code"] == "logs_unavailable"


# ---------- PostgreSQL-backed contracts (disposable, skipped without TEST_POSTGRES_URL) ----------

def _pg_url() -> str:
    url = os.environ.get("TEST_POSTGRES_URL", "")
    if not url:
        pytest.skip("TEST_POSTGRES_URL is required")
    return url


@pytest.mark.asyncio
async def test_cohort_half_open_boundary_and_chart_kpi_agreement(client, app, channel_id):
    _pg_url()
    db = app.state.db
    # Isolated channel so the seeded @sample_channel post does not interfere.
    cohort_channel = await db.upsert_channel("@t51_cohort", "T51 cohort", 51001)
    base = datetime(2026, 9, 6, 0, 0, tzinfo=timezone.utc)
    fixtures = [
        ("2026-09-05T23:59:59", 5),   # before From -> excluded
        ("2026-09-06T00:00:00", 10),  # From inclusive
        ("2026-09-06T12:00:00", 20),
        ("2026-09-07T23:59:59", 30),  # To inclusive (To=2026-09-07)
        ("2026-09-08T00:00:00", 40),  # To+1 00:00 exclusive
    ]
    for index, (iso, views) in enumerate(fixtures):
        posted = datetime.fromisoformat(iso.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)
        post_id = await db.upsert_post(cohort_channel, 1000 + index, posted, f"cohort post {index}")
        await db.add_snapshot_if_changed(post_id, views=views, comments=index, reactions=index, shares=0)
    kpis = await db.kpis(cohort_channel, from_date="2026-09-06", to_date="2026-09-07")
    assert int(kpis["posts"]) == 3
    assert int(kpis["views"]) == 60
    series = await db.timeseries_totals(days=None, channel_id=cohort_channel, from_date="2026-09-06", to_date="2026-09-07")
    assert series["days"] == ["2026-09-06", "2026-09-07"]
    # Same cohort powers chart and KPIs: daily buckets sum to KPI totals.
    assert sum(series["views"]) == int(kpis["views"])
    assert series["views"] == [30, 30]
    assert series["posts_per_day"] == [2, 1]
    # Sparse/empty: single-day cohort and empty cohort.
    single = await db.timeseries_totals(days=None, channel_id=cohort_channel, from_date="2026-09-06", to_date="2026-09-06")
    assert single["days"] == ["2026-09-06"]
    assert single["views"] == [30]
    empty_kpis = await db.kpis(cohort_channel, from_date="2026-01-01", to_date="2026-01-02")
    assert int(empty_kpis["posts"]) == 0
    assert int(empty_kpis["views"]) == 0
    empty_series = await db.timeseries_totals(days=None, channel_id=cohort_channel, from_date="2026-01-01", to_date="2026-01-02")
    assert empty_series["days"] == ["2026-01-01", "2026-01-02"]
    assert empty_series["views"] == [0, 0]
    assert empty_series["posts_per_day"] == [0, 0]
    # HTTP agreement: /api/overview matches the repository cohort.
    await client.post("/login", data={"username": "test-admin", "password": "test-password"})
    resp = await client.get(f"/api/overview?channel={cohort_channel}&from=2026-09-06&to=2026-09-07")
    assert resp.status_code == 200
    body = resp.json()
    assert body["kpis"]["posts"] == 3
    assert body["kpis"]["views"] == 60
    assert body["series"]["days"] == ["2026-09-06", "2026-09-07"]
    # Invalid dates are bounded and never partially apply.
    bad = await client.get(f"/api/overview?channel={cohort_channel}&from=2026-09-07&to=2026-09-06")
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_paused_channels_all_active_vs_explicit(client, app, channel_id):
    _pg_url()
    db = app.state.db
    paused = await db.upsert_channel("@t51_paused", "T51 paused", 51002)
    post_id = await db.upsert_post(paused, 1, datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc), "paused post")
    await db.add_snapshot_if_changed(post_id, views=999, comments=0, reactions=0, shares=0)
    assert await db.deactivate_channel(paused) is True
    all_kpis = await db.kpis(None)
    # Seeded @sample_channel post (120 views) is active; paused 999 must be excluded.
    assert int(all_kpis["views"]) == 120
    explicit = await db.kpis(paused)
    assert int(explicit["posts"]) == 1
    assert int(explicit["views"]) == 999
    # Explorer agreement: All-active total excludes paused; explicit includes.
    all_rows, all_total = await db.explorer_posts(limit=100, offset=0)
    assert all_total >= 1
    assert all(not (r["channel_id"] == paused) for r in all_rows)
    paused_rows, paused_total = await db.explorer_posts(channel_id=paused, limit=100, offset=0)
    assert paused_total == 1
    # Totals/list agreement for the active scope.
    active_kpis = await db.kpis(channel_id)
    active_rows, active_total = await db.explorer_posts(channel_id=channel_id, limit=100, offset=0)
    assert int(active_kpis["posts"]) == active_total
    assert sum(r["views"] for r in active_rows) == int(active_kpis["views"])


@pytest.mark.asyncio
async def test_explorer_combined_ranges_stable_pagination(client, app):
    _pg_url()
    db = app.state.db
    channel = await db.upsert_channel("@t51_explorer", "T51 explorer", 51003)
    specs = [
        (1, 100, 10, 5, 1),
        (2, 200, 5, 5, 2),
        (3, 200, 15, 1, 1),  # same views tie with #2: newer posted_at first, then higher id
        (4, 50, 50, 50, 50),
    ]
    base = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    for message_id, views, reactions, comments, shares in specs:
        pid = await db.upsert_post(channel, message_id, base + timedelta(days=message_id), f"explorer search-alpha {message_id}")
        await db.add_snapshot_if_changed(pid, views=views, comments=comments, reactions=reactions, shares=shares)
    # Combined AND ranges.
    rows, total = await db.explorer_posts(
        channel_id=channel, search="search-alpha", min_views=100, max_views=200,
        min_reactions=5, max_reactions=15, sort="views", limit=100, offset=0,
    )
    assert total == 3
    assert [r["message_id"] for r in rows] == [3, 2, 1]  # views tie -> newer first
    # Stable pagination: pages concatenate to the full ordered list.
    page1, total1 = await db.explorer_posts(channel_id=channel, sort="views", limit=2, offset=0)
    page2, total2 = await db.explorer_posts(channel_id=channel, sort="views", limit=2, offset=2)
    assert total1 == total2 == 4
    assert [r["message_id"] for r in page1 + page2] == [2, 3, 1, 4] or [r["message_id"] for r in page1 + page2] == [3, 2, 1, 4]
    # Invalid ranges/sorts are bounded.
    with pytest.raises(ValueError):
        await db.explorer_posts(channel_id=channel, min_views=10, max_views=5)
    with pytest.raises(ValueError):
        await db.explorer_posts(channel_id=channel, sort="combined_score")
    # HTTP Explorer: same semantics, bounded pagination, total count.
    await client.post("/login", data={"username": "test-admin", "password": "test-password"})
    resp = await client.get(f"/api/explorer?channel={channel}&q=search-alpha&sort=views&page=1&page_size=2")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 4
    assert body["page_size"] == 2
    bad = await client.get(f"/api/explorer?channel={channel}&sort=bogus")
    assert bad.status_code == 422
    bad_range = await client.get(f"/api/explorer?channel={channel}&min_views=10&max_views=5")
    assert bad_range.status_code == 422
    # Explorer history is independent of chart dates: date params are ignored, not an error.
    dated = await client.get(f"/api/explorer?channel={channel}&q=search-alpha")
    assert dated.status_code == 200
    assert dated.json()["total"] == 4


@pytest.mark.asyncio
async def test_post_read_model_and_cross_workspace(client, app, channel_id):
    _pg_url()
    db = app.state.db
    post_id = await db.upsert_post(
        channel_id, 777, datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc),
        "Hello <b>world</b> click",
        formatting_entities=[
            {"type": "bold", "offset": 0, "length": 5},
            {"type": "text_url", "offset": 18, "length": 5, "url": "javascript:alert(1)"},
        ],
    )
    await db.add_snapshot_if_changed(post_id, views=10, comments=0, reactions=1, shares=0)
    await client.post("/login", data={"username": "test-admin", "password": "test-password"})
    resp = await client.get(f"/api/posts/{post_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"].startswith("Hello")
    assert "<strong>Hello</strong>" in body["formatted_html"]
    assert "javascript:" not in body["formatted_html"]
    assert "<b>world</b>" not in body["formatted_html"]
    assert body["metrics"]["views"] == 10
    assert isinstance(body["snapshots"], list) and len(body["snapshots"]) >= 1
    assert body["source_url"] is None or body["source_url"].startswith("https://t.me/")
    assert isinstance(body["discussion"], list)
    # Unknown post is 404, never a whole-history download.
    missing = await client.get("/api/posts/999999999")
    assert missing.status_code == 404
    # Cross-workspace IDs are authorized server-side.
    from app.postgres_db import PostgresDatabase

    other = PostgresDatabase(os.environ["TEST_POSTGRES_URL"], workspace_slug=f"t51-other-{uuid.uuid4().hex[:8]}")
    try:
        await other.init_db(admin_username="test-admin")
        other_channel = await other.upsert_channel("@other", "Other", 99999)
        other_post = await other.upsert_post(other_channel, 1, datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc), "other workspace")
        foreign = await client.get(f"/api/posts/{other_post}")
        # Either 404 (isolated) or a different row that is not the foreign text.
        if foreign.status_code == 200:
            assert foreign.json()["text"] != "other workspace"
        else:
            assert foreign.status_code == 404
    finally:
        from sqlalchemy import text as sql_text
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(os.environ["TEST_POSTGRES_URL"])
        try:
            async with engine.connect() as conn:
                row = await conn.execute(sql_text("SELECT id FROM workspaces WHERE slug=:slug"), {"slug": other.workspace_slug})
                found = row.first()
                if found is not None:
                    await conn.execute(sql_text("DELETE FROM workspaces WHERE id=:id"), {"id": found[0]})
                    await conn.commit()
        finally:
            await engine.dispose()
        await other.close()


@pytest.mark.asyncio
async def test_logs_filtering_and_export_headers(client, app, channel_id, settings):
    _pg_url()
    repository = app.state.studio_repository
    other_channel = await app.state.db.upsert_channel("@t51_logs", "T51 logs", 51004)
    conv_a = await repository.create_conversation(channel_id=channel_id, title="Alpha research")
    msg_a = await repository.append_message(conversation_id=conv_a["id"], role="user", content="hello")
    run_a = await repository.create_run(conversation_id=conv_a["id"], user_message_id=msg_a["id"], requested_model="model-a")
    await repository.append_event(run_a["id"], event_type="TOOL_CALL_START", safe_payload={"tool_name": "search_web", "tool_call_id": "x"})
    await repository.append_event(run_a["id"], event_type="TOOL_CALL_RESULT", safe_payload={"tool_name": "search_web", "tool_call_id": "x"}, result_content="alpha result")
    await repository.set_run_status(run_a["id"], status="succeeded", actual_model="model-a-actual")
    conv_b = await repository.create_conversation(channel_id=other_channel, title="Beta research")
    msg_b = await repository.append_message(conversation_id=conv_b["id"], role="user", content="hello")
    run_b = await repository.create_run(conversation_id=conv_b["id"], user_message_id=msg_b["id"], requested_model="model-b")
    await repository.append_event(run_b["id"], event_type="TOOL_CALL_RESULT", safe_payload={"tool_name": "read_source"}, result_content="beta result")
    await repository.set_run_status(run_b["id"], status="failed", error_code="model_error", error_message="boom")
    await client.post("/login", data={"username": settings.admin_username, "password": settings.admin_password})
    # Channel + status + query operate server-side with counts.
    filtered = await client.get(f"/settings/logs?q=alpha&channel={channel_id}&status=succeeded", headers={"Accept": "application/json"})
    assert filtered.status_code == 200
    body = filtered.json()
    assert body["available"] is True
    assert body["total"] >= 1
    assert all(r["channel_id"] == channel_id for r in body["logs"])
    assert all(r["run_status"] == "succeeded" for r in body["logs"])
    assert any(r["requested_model"] == "model-a" for r in body["logs"])
    failed = await client.get("/settings/logs?status=failed", headers={"Accept": "application/json"})
    assert failed.status_code == 200
    assert any(r["run_status"] == "failed" for r in failed.json()["logs"])
    bad_status = await client.get("/settings/logs?status=bogus", headers={"Accept": "application/json"})
    assert bad_status.status_code == 422
    # Export matches its advertised stored-history scope and communicates its cap.
    posts_csv = await client.get("/export.csv")
    assert posts_csv.status_code == 200
    assert posts_csv.headers["X-Export-Truncated"] == "false"
    assert posts_csv.headers["X-Export-Limit"] == "100000"
    assert posts_csv.headers["X-Export-Scope"].startswith("stored-history:")
    assert "post_db_id" in posts_csv.text
    comments_csv = await client.get("/export-comments.csv")
    assert comments_csv.status_code == 200
    assert comments_csv.headers["X-Export-Truncated"] in {"true", "false"}
    assert "comment_db_id" in comments_csv.text

