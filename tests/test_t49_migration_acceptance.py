"""T49 integrated acceptance for the finalized design migration (T44–T53).

Proves the combined journey on one tree with real adapters and isolated
synthetic persistence: Overview (dates/KPIs/chart/sync/export) → Post
Explorer → reader → channel-bound Studio (conversations, formatted
draft/version/copy, profile, System Prompt) → Settings/logs → return.
Reloads reconcile from canonical saved state; channels stay isolated;
conflicts preserve edits; secrets never echo.

Unavailable here by environment (labelled, not passing): disposable
PostgreSQL (no CREATEDB for the test role), a live browser (layout/phase
screenshots, device keyboard, safe areas, 200% zoom observation) and real
provider/channel calls. Those need their own session authorization.
"""

from __future__ import annotations

import asyncio
import html as html_module
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.studio.repository import MemoryStudioRepository
from app.studio.service import StudioService
from app.web.dependencies import WorkspaceContext
from app.web.routes import create_app


ROOT = Path(__file__).resolve().parents[1]

_TEST_FERNET_KEY = "Wl0-cvos82PSFL7U0PbD7SrvsEJRKq6I1Y_APkFy3iM="


def _dt(day: str, hour: int = 10) -> str:
    return f"{day} {hour:02d}:00:00+00:00"


class _JourneyDB:
    """Synthetic web archive: two channels, dated posts, snapshots, comments."""

    def __init__(self) -> None:
        self.user_id = "00000000-0000-0000-0000-000000000001"
        self.workspace_id = "00000000-0000-0000-0000-000000000002"
        self.workspace_slug = "community"
        self.active_channel_count = 2
        self.channels = [
            {"id": 1, "identifier": "@chan_one", "title": "Channel One", "chat_id": -1001, "active": True},
            {"id": 2, "identifier": "@chan_two", "title": "Channel Two", "chat_id": -1002, "active": True},
        ]
        self.posts = [
            {
                "id": 1, "message_id": 101, "channel_id": 1, "identifier": "@chan_one",
                "chat_id": -1001, "posted_at": _dt("2026-08-24"), "text": "Hello markets post",
                "formatting_entities": [{"type": "bold", "offset": 6, "length": 7}],
                "views": 1000, "reactions": 50, "comments": 10, "shares": 5,
                "collected_comments": 1, "updated_at": _dt("2026-09-06", 12), "views_delta": 0,
            },
            {
                "id": 2, "message_id": 102, "channel_id": 1, "identifier": "@chan_one",
                "chat_id": -1001, "posted_at": _dt("2026-08-30"), "text": "second markets post",
                "formatting_entities": [], "views": 2000, "reactions": 60, "comments": 20,
                "shares": 6, "collected_comments": 0, "updated_at": _dt("2026-09-06", 12), "views_delta": 0,
            },
            {
                "id": 3, "message_id": 103, "channel_id": 2, "identifier": "@chan_two",
                "chat_id": -1002, "posted_at": _dt("2026-09-06"), "text": "weather post",
                "formatting_entities": [], "views": 5120, "reactions": 184, "comments": 37,
                "shares": 12, "collected_comments": 2, "updated_at": _dt("2026-09-06", 12), "views_delta": 0,
            },
        ]

    def _scoped(self, channel_id=None, from_date=None, to_date=None):
        from app.postgres_db import cohort_bounds

        start, end_exclusive = cohort_bounds(from_date, to_date)
        rows = [r for r in self.posts if channel_id is None or r["channel_id"] == channel_id]
        if start is not None:
            rows = [r for r in rows if r["posted_at"] >= start.strftime("%Y-%m-%d %H:%M:%S+00:00")]
        if end_exclusive is not None:
            rows = [r for r in rows if r["posted_at"] < end_exclusive.strftime("%Y-%m-%d %H:%M:%S+00:00")]
        return rows

    async def get_channels(self):
        return self.channels

    async def kpis(self, channel_id=None, from_date=None, to_date=None):
        rows = self._scoped(channel_id, from_date, to_date)
        return {
            "posts": len(rows), "views": sum(r["views"] for r in rows),
            "reactions": sum(r["reactions"] for r in rows), "comments": sum(r["comments"] for r in rows),
            "shares": sum(r["shares"] for r in rows), "collected_comments": sum(r["collected_comments"] for r in rows),
            "last_poll": "2026-09-06 12:00:00+00:00",
        }

    async def timeseries_totals(self, days=None, channel_id=None, from_date=None, to_date=None):
        from datetime import timedelta

        from app.postgres_db import cohort_bounds

        rows = self._scoped(channel_id, from_date, to_date)
        start, end_exclusive = cohort_bounds(from_date, to_date)
        anchor = datetime(2026, 9, 6, tzinfo=timezone.utc).date()
        if start is not None and end_exclusive is not None:
            first, last = start.date(), (end_exclusive - timedelta(days=1)).date()
        elif start is not None:
            first, last = start.date(), anchor
        else:
            span_days = days if days is not None else 14
            last = anchor
            first = last - timedelta(days=span_days - 1)
        span = max(1, (last - first).days + 1)
        labels = [((datetime(first.year, first.month, first.day, tzinfo=timezone.utc)) + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(span)]
        totals = {label: [0, 0, 0, 0, 0] for label in labels}
        for row in rows:
            key = row["posted_at"][:10]
            if key in totals:
                totals[key][0] += 1
                totals[key][1] += row["views"]
                totals[key][2] += row["reactions"]
                totals[key][3] += row["comments"]
                totals[key][4] += row["shares"]
        return {
            "days": labels, "views": [totals[label][1] for label in labels],
            "reactions": [totals[label][2] for label in labels],
            "comments": [totals[label][3] for label in labels],
            "shares": [totals[label][4] for label in labels],
            "posts_per_day": [totals[label][0] for label in labels],
        }

    async def latest_stats(self, channel_id=None, order="date", limit=100, offset=0):
        rows = [r for r in self.posts if channel_id is None or r["channel_id"] == channel_id]
        key = {"date": "posted_at", "views": "views", "reactions": "reactions",
               "comments": "comments", "shares": "shares"}[order]
        rows = sorted(rows, key=lambda r: (r[key], r["id"]), reverse=True)
        return [dict(r) for r in rows[offset:offset + limit]]

    async def explorer_posts(self, channel_id=None, channel_ids=None, search=None, sort="date",
                             min_views=None, max_views=None, min_reactions=None, max_reactions=None,
                             min_comments=None, max_comments=None, min_shares=None, max_shares=None,
                             limit=50, offset=0):
        rows = list(self.posts)
        if channel_id is not None:
            rows = [r for r in rows if r["channel_id"] == channel_id]
        if channel_ids:
            rows = [r for r in rows if r["channel_id"] in set(channel_ids)]
        if search:
            rows = [r for r in rows if search.lower() in (r["text"] or "").lower()]
        for metric, low, high in (("views", min_views, max_views), ("reactions", min_reactions, max_reactions),
                                  ("comments", min_comments, max_comments), ("shares", min_shares, max_shares)):
            if low is not None:
                rows = [r for r in rows if r[metric] >= low]
            if high is not None:
                rows = [r for r in rows if r[metric] <= high]
        key = sort if sort != "date" else "posted_at"
        rows = sorted(rows, key=lambda r: (r[key], r["posted_at"], r["id"]), reverse=True)
        return [dict(r) for r in rows[offset:offset + limit]], len(rows)

    async def post_row(self, post_id):
        for row in self.posts:
            if row["id"] == post_id:
                return dict(row) | {"channel_title": "Channel One" if row["channel_id"] == 1 else "Channel Two"}
        return None

    async def post_history(self, post_id):
        if post_id == 1:
            return [
                {"taken_at": _dt("2026-09-06", 12), "views": 1000, "comments": 10, "reactions": 50, "shares": 5},
                {"taken_at": _dt("2026-09-05", 12), "views": 900, "comments": 9, "reactions": 45, "shares": 4},
            ]
        return []

    async def comments_for_post(self, post_id):
        if post_id == 1:
            return [
                {"id": 1, "telegram_message_id": 201, "sender_name": "Reader", "sender_username": "reader",
                 "posted_at": _dt("2026-09-06", 13), "edited_at": "", "text": "Nice post", "reactions": 2,
                 "discussion_chat_id": -1001, "discussion_username": "chan_one"},
            ]
        return []

    async def all_comments(self, channel_id=None, limit=100000):
        return []


class _JourneyCollector:
    client = object()

    def __init__(self, db):
        self.db = db
        self.workspace_settings = None
        self.scheduled: list[str] = []

    def schedule_poll(self, reason="manual"):
        self.scheduled.append(reason)
        return asyncio.create_task(asyncio.sleep(0))


def _make_journey_app(tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=str(tmp_path),
        admin_username="admin",
        admin_password="pw",
        session_secret="secret",
        studio_test_mode=True,
        telegram_session_encryption_key=_TEST_FERNET_KEY,
        channels="@chan_one",
    )
    db = _JourneyDB()
    collector = _JourneyCollector(db)
    app = create_app(collector, settings)
    studio_repository = MemoryStudioRepository(workspace_id="t49-journey")
    studio_repository.channels = [
        {"id": 1, "identifier": "@chan_one", "title": "Channel One", "active": True},
        {"id": 2, "identifier": "@chan_two", "title": "Channel Two", "active": True},
    ]
    app.state.studio_repository = studio_repository
    app.state.studio_service = StudioService(studio_repository, settings)
    app.state.workspace_context = WorkspaceContext(
        user_id=db.user_id,
        workspace_id=db.workspace_id,
        workspace_slug="community",
        role="owner",
    )
    return app, collector, db, studio_repository


def _chart_payload(page_text: str) -> dict:
    match = re.search(r"<canvas id=\"trendChart\" data-chart='(.*?)'", page_text, flags=re.DOTALL)
    assert match, "trend chart canvas with embedded data must exist"
    return json.loads(html_module.unescape(match.group(1)))


@pytest.mark.asyncio
async def test_journey_authentication_and_overview_shell(tmp_path):
    app, _, _, _ = _make_journey_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        anonymous = await client.get("/", follow_redirects=False)
        assert anonymous.status_code == 303
        login = await client.post("/login", data={"username": "admin", "password": "pw"})
        assert login.status_code == 303
        page = await client.get("/")
        assert page.status_code == 200
        # One shared shell ghost, finalized Overview controls, Explorer, chart.
        assert page.text.count('id="ghost-trigger"') == 1
        assert 'data-ghost-src="/static/brand/tgstudio-ghost-overview.svg' in page.text
        assert 'name="from"' in page.text and 'name="to"' in page.text
        assert 'id="chartMetric"' in page.text
        assert "<h2>Post Explorer</h2>" in page.text
        assert 'id="postReader"' in page.text
        assert "Sync now" in page.text


@pytest.mark.asyncio
async def test_journey_overview_cohort_chart_and_csv(tmp_path):
    app, _, _, _ = _make_journey_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/login", data={"username": "admin", "password": "pw"})
        page = await client.get("/?from=2026-08-24&to=2026-09-06")
        assert page.status_code == 200
        assert "8,120" in page.text
        chart = _chart_payload(page.text)
        assert chart["labels"][0] == "2026-08-24" and chart["labels"][-1] == "2026-09-06"
        assert sum(chart["views"]) == 8120
        # The single day selects exactly one post; an empty window recovers.
        single = await client.get("/?from=2026-09-06&to=2026-09-06")
        assert "5,120" in single.text
        empty = await client.get("/?from=2026-01-01&to=2026-01-02")
        assert "No posts in this window" in empty.text
        # Downloaded CSV content is actually checked: headers, rows, scope.
        posts = await client.get("/export.csv?channel=1")
        assert posts.status_code == 200
        assert posts.headers["X-Export-Scope"] == "stored-history:channel-1"
        assert posts.headers["X-Export-Truncated"] == "false"
        lines = posts.text.splitlines()
        assert lines[0].startswith("post_db_id,message_id,channel,posted_at_utc")
        assert len(lines) == 3
        assert "second markets post" in posts.text
        comments = await client.get("/export-comments.csv?channel=1")
        assert comments.status_code == 200
        assert comments.text.splitlines()[0].startswith("comment_db_id,post_db_id")


@pytest.mark.asyncio
async def test_journey_explorer_reader_and_studio_handoff_contract(tmp_path):
    app, _, _, _ = _make_journey_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/login", data={"username": "admin", "password": "pw"})
        found = await client.get("/api/explorer", params={"q": "markets", "sort": "views"})
        assert found.status_code == 200
        assert found.json()["total"] == 2
        detail = await client.get("/api/posts/1")
        assert detail.status_code == 200
        body = detail.json()
        assert "<strong>" in body["formatted_html"] and "markets" in body["formatted_html"]
        assert body["channel_id"] == 1
        assert [c["id"] for c in body["discussion"]] == [1]
        reader = await client.get("/post/1")
        assert reader.status_code == 200
        assert "Explore in Studio" in reader.text
        # The handoff contract both sides implement: storage key plus shape.
        assert "tg-studio:prefill" in (ROOT / "app/web/static/app.js").read_text(encoding="utf-8")
        main_source = (ROOT / "studio-frontend/src/main.tsx").read_text(encoding="utf-8")
        assert "consumeStoredPrefill" in main_source
        assert "PREFILL_MAX_TEXT" in (ROOT / "studio-frontend/src/api.ts").read_text(encoding="utf-8")


async def _studio_login(client):
    await client.post("/login", data={"username": "admin", "password": "pw"})
    page = await client.get("/studio")
    assert page.status_code == 200
    match = re.search(r'<meta name="studio-csrf-token" content="([^"]+)"', page.text)
    assert match, "studio page must expose a CSRF token"
    return match.group(1)


@pytest.mark.asyncio
async def test_journey_channel_bound_studio_conversations_and_draft(tmp_path):
    app, _, _, studio_repository = _make_journey_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        token = await _studio_login(client)
        headers = {"x-csrf-token": token}
        empty = await client.get("/studio/api/bootstrap", params={"channel_id": 1})
        assert empty.status_code == 200
        assert empty.json()["selected_channel_id"] == 1
        assert empty.json()["conversations"] == []
        created = await client.post("/studio/api/conversations", json={"channel_id": 1, "title": "Journey"}, headers=headers)
        assert created.status_code == 200
        conversation_id = created.json()["conversation"]["id"]
        # Channel B sees none of channel A's conversations.
        other = await client.get("/studio/api/bootstrap", params={"channel_id": 2})
        assert other.json()["conversations"] == []
        # Formatted draft lifecycle with revision conflicts preserving edits.
        draft = await studio_repository.create_draft(
            conversation_id=uuid.UUID(conversation_id), channel_id=1,
            payload={"working_title": "Journey draft", "body": "Hello **world**", "creative": True},
        )
        current = await client.get(f"/studio/api/conversations/{conversation_id}/draft")
        assert current.status_code == 200
        assert current.json()["draft"]["body"] == "Hello **world**"
        saved = await client.patch(
            f"/studio/api/drafts/{draft['id']}",
            json={"expected_revision": draft["revision"], "body": "Hello **edited**"},
            headers=headers,
        )
        assert saved.status_code == 200
        assert saved.json()["draft"]["body"] == "Hello **edited**"
        conflict = await client.patch(
            f"/studio/api/drafts/{draft['id']}",
            json={"expected_revision": draft["revision"], "body": "stale edit"},
            headers=headers,
        )
        assert conflict.status_code == 409
        assert conflict.json()["local_draft"]["body"] == "stale edit"
        branched = await client.patch(
            f"/studio/api/drafts/{draft['id']}",
            json={"expected_revision": draft["revision"] + 1, "body": "Hello **branched**", "save_as_new_version": True},
            headers=headers,
        )
        assert branched.status_code == 200
        versions = await client.get(f"/studio/api/drafts/{draft['id']}/versions")
        assert versions.status_code == 200
        assert len(versions.json()["versions"]) >= 2
        # Reload reconciles from canonical saved state.
        again = await client.get(f"/studio/api/conversations/{conversation_id}/draft")
        assert again.json()["draft"]["body"] == "Hello **branched**"


@pytest.mark.asyncio
async def test_journey_profile_prompt_consent_and_settings(tmp_path):
    app, _, _, _ = _make_journey_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        token = await _studio_login(client)
        headers = {"x-csrf-token": token}
        profile = await client.get("/studio/api/profile", params={"channel_id": 1})
        assert profile.status_code == 200
        assert profile.json()["profile"] is None
        # The profile API carries no evidence IDs yet: the reader surface
        # waits for the T37–T39 backend instead of inventing excerpts.
        saved = await client.put(
            "/studio/api/profile",
            json={"channel_id": 1, "expected_version": 0, "topics_text": "Markets",
                    "editorial_text": "Cite sources.", "style_text": "Brief."},
            headers=headers,
        )
        assert saved.status_code == 200
        assert saved.json()["profile"]["version"] == 1
        stale = await client.put(
            "/studio/api/profile",
            json={"channel_id": 1, "expected_version": 0, "topics_text": "Other",
                    "editorial_text": "", "style_text": ""},
            headers=headers,
        )
        assert stale.status_code == 409
        assert stale.json()["server_profile"]["topics_text"] == "Markets"
        prompt = await client.get("/studio/api/settings", params={"channel_id": 1})
        assert prompt.json()["system_prompt"] == ""
        updated = await client.patch("/studio/api/settings", json={"channel_id": 1, "system_prompt": "Be brief."}, headers=headers)
        assert updated.json()["system_prompt"] == "Be brief."
        # Channel B keeps its own empty prompt: nothing transfers.
        assert (await client.get("/studio/api/settings", params={"channel_id": 2})).json()["system_prompt"] == ""
        consent = await client.get("/studio/api/consent")
        assert consent.status_code == 200
        assert consent.json()["consent"]["granted"] is True
        settings_page = await client.get("/settings")
        assert settings_page.status_code == 200
        assert "Global settings apply" in settings_page.text or "scope" in settings_page.text.lower()
        # Secrets stay write-only: empty password inputs, never echoed values.
        assert 'type="password"' in settings_page.text
        assert "never shown" in settings_page.text
        assert "Leave empty to keep" in settings_page.text
        logs = await client.get("/settings/logs")
        assert logs.status_code == 200
        health = await client.get("/studio/api/research/health")
        assert health.status_code in (200, 503)


def test_product_has_no_review_controls_or_demo_machinery():
    pages = [
        (ROOT / "app/web/templates/dashboard.html").read_text(encoding="utf-8"),
        (ROOT / "app/web/templates/post.html").read_text(encoding="utf-8"),
        (ROOT / "app/web/templates/settings.html").read_text(encoding="utf-8"),
        (ROOT / "app/web/templates/studio.html").read_text(encoding="utf-8"),
    ]
    scripts = [
        (ROOT / "app/web/static/app.js").read_text(encoding="utf-8"),
        (ROOT / "app/web/static/ghost.js").read_text(encoding="utf-8"),
        (ROOT / "app/web/static/ui-controls.js").read_text(encoding="utf-8"),
    ]
    bundle = (ROOT / "app/web/static/studio-dist/assets/studio.js").read_text(encoding="utf-8")
    for banned in ("Play example run", "demo-fill", "fictional-data", "Motion on/off",
                    "Try in draft", "States review", "demo credential"):
        for text in pages + scripts:
            assert banned not in text, banned
        assert banned not in bundle, banned


def test_mascot_scenes_are_singular_with_clear_lens():
    brand = ROOT / "app/web/static/brand"
    scenes = ["tgstudio-ghost-overview.svg", "tgstudio-ghost-studio.svg", "tgstudio-ghost-settings.svg"]
    for name in scenes:
        content = (brand / name).read_text(encoding="utf-8")
        assert "<svg" in content
    overview = (brand / scenes[0]).read_text(encoding="utf-8")
    assert 'r="72"' in overview
    ghost = (ROOT / "app/web/static/ghost.js").read_text(encoding="utf-8")
    assert "removeEventListener" in ghost
    assert "prefers-reduced-motion" in ghost
    for template in ("dashboard.html", "studio.html", "settings.html", "settings_logs.html", "post.html"):
        text = (ROOT / "app/web/templates" / template).read_text(encoding="utf-8")
        assert text.count('id="ghost-trigger"') <= 1, template
