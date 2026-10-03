"""T52 Post Explorer, formatted reader and Studio handoff acceptance tests.

One Sort dropdown (no sortable headers, no combined score); search, channel
subset and all four metric ranges narrow the Overview scope server-side with
count/page state. The reader shows full safe Telegram formatting, discussion,
latest metrics with separately labelled collection snapshots and a validated
source link, with previous/next, Escape/focus return and list restoration.
Explore in Studio passes an authorized post/channel reference (never raw
channel content in URLs) and prefills without sending.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from app.collector import Collector
from app.config import Settings
from app.web.dependencies import WorkspaceContext
from app.web.routes import create_app


ROOT = Path(__file__).resolve().parents[1]


def _dt(day: str, hour: int = 10) -> str:
    return f"{day} {hour:02d}:00:00+00:00"


def _post(pid, channel, day, text, views, reactions, comments, shares, entities=None):
    return {
        "id": pid,
        "message_id": 100 + pid,
        "channel_id": channel,
        "identifier": "@chan_one" if channel == 1 else "@chan_two",
        "channel_title": "Channel One" if channel == 1 else "Channel Two",
        "chat_id": -1001 if channel == 1 else -1002,
        "posted_at": _dt(day),
        "text": text,
        "formatting_entities": entities or [],
        "views": views,
        "reactions": reactions,
        "comments": comments,
        "shares": shares,
        "collected_comments": 1,
        "updated_at": _dt("2026-09-06", 12),
        "views_delta": 0,
    }


class _T52FakeDB:
    def __init__(self) -> None:
        self.user_id = "00000000-0000-0000-0000-000000000001"
        self.workspace_id = "00000000-0000-0000-0000-000000000002"
        self.channels = [
            {"id": 1, "identifier": "@chan_one", "title": "Channel One", "chat_id": -1001, "active": True},
            {"id": 2, "identifier": "@chan_two", "title": "Channel Two", "chat_id": -1002, "active": True},
        ]
        self.posts = [
            _post(1, 1, "2026-08-24", "Hello alpha post about markets", 1000, 50, 10, 5,
                  [{"type": "bold", "offset": 0, "length": 5}]),
            _post(2, 1, "2026-08-30", "beta post about markets", 2000, 60, 20, 6),
            _post(3, 2, "2026-09-06", "gamma post about weather", 5120, 184, 37, 12),
            _post(4, 2, "2026-09-05", "delta post about markets", 2000, 30, 5, 2),
            _post(5, 1, "2026-09-04", "", 10, 1, 0, 0),
            _post(6, 2, "2026-09-03", "epsilon notes", 300, 12, 3, 1),
        ]
        self.snaps = {
            1: [
                {"taken_at": _dt("2026-09-06", 12), "views": 1000, "comments": 10, "reactions": 50, "shares": 5},
                {"taken_at": _dt("2026-09-05", 12), "views": 900, "comments": 9, "reactions": 45, "shares": 4},
            ],
        }
        self.discussion = {
            1: [
                {"id": 1, "telegram_message_id": 201, "sender_name": "Reader", "sender_username": "reader",
                 "posted_at": _dt("2026-09-06", 13), "edited_at": "", "text": "Nice post",
                 "reactions": 2, "discussion_chat_id": -1001, "discussion_username": "chan_one"},
                {"id": 2, "telegram_message_id": 202, "sender_name": "Gone", "sender_username": "",
                 "posted_at": _dt("2026-09-06", 14), "edited_at": "", "text": "removed",
                 "reactions": 0, "is_deleted": True, "discussion_chat_id": -1001, "discussion_username": "chan_one"},
            ],
        }

    async def get_channels(self):
        return self.channels

    async def kpis(self, channel_id=None, from_date=None, to_date=None):
        rows = [r for r in self.posts if channel_id is None or r["channel_id"] == channel_id]
        return {
            "posts": len(rows), "views": sum(r["views"] for r in rows),
            "reactions": sum(r["reactions"] for r in rows), "comments": sum(r["comments"] for r in rows),
            "shares": sum(r["shares"] for r in rows), "collected_comments": len(rows),
            "last_poll": "2026-09-06 12:00:00+00:00",
        }

    async def timeseries_totals(self, days=None, channel_id=None, from_date=None, to_date=None):
        return {"days": ["2026-09-06"], "views": [5120], "reactions": [184],
                "comments": [37], "shares": [12], "posts_per_day": [1]}

    async def latest_stats(self, channel_id=None, order="date", limit=100, offset=0):
        rows = [r for r in self.posts if channel_id is None or r["channel_id"] == channel_id]
        key = {"date": "posted_at", "views": "views", "reactions": "reactions",
               "comments": "comments", "shares": "shares"}[order]
        rows = sorted(rows, key=lambda r: (r[key], r["id"]), reverse=True)
        return [dict(r) for r in rows[offset:offset + limit]]

    def _filtered(self, channel_id=None, channel_ids=None, search=None,
                  min_views=None, max_views=None, min_reactions=None, max_reactions=None,
                  min_comments=None, max_comments=None, min_shares=None, max_shares=None):
        rows = list(self.posts)
        if channel_id is not None:
            rows = [r for r in rows if r["channel_id"] == channel_id]
        if channel_ids:
            rows = [r for r in rows if r["channel_id"] in set(channel_ids)]
        if search:
            rows = [r for r in rows if search.lower() in (r["text"] or "").lower()]
        for metric, low, high in (
            ("views", min_views, max_views), ("reactions", min_reactions, max_reactions),
            ("comments", min_comments, max_comments), ("shares", min_shares, max_shares),
        ):
            if low is not None:
                rows = [r for r in rows if r[metric] >= low]
            if high is not None:
                rows = [r for r in rows if r[metric] <= high]
        return rows

    async def explorer_posts(self, channel_id=None, channel_ids=None, search=None, sort="date",
                             min_views=None, max_views=None, min_reactions=None, max_reactions=None,
                             min_comments=None, max_comments=None, min_shares=None, max_shares=None,
                             limit=50, offset=0):
        rows = self._filtered(channel_id, channel_ids, search,
                              min_views, max_views, min_reactions, max_reactions,
                              min_comments, max_comments, min_shares, max_shares)
        # Same contract as PostgresDatabase: metric DESC, posted_at DESC, id DESC.
        key = sort if sort != "date" else "posted_at"
        rows = sorted(rows, key=lambda r: (r[key], r["posted_at"], r["id"]), reverse=True)
        total = len(rows)
        return [dict(r) for r in rows[offset:offset + limit]], total

    async def post_row(self, post_id):
        for row in self.posts:
            if row["id"] == post_id:
                return dict(row)
        return None

    async def post_history(self, post_id):
        return list(self.snaps.get(post_id, []))

    async def comments_for_post(self, post_id):
        return [dict(c) for c in self.discussion.get(post_id, [])]

    async def all_comments(self, channel_id=None, limit=100000):
        return []


class _T52Collector:
    client = object()

    def __init__(self, db):
        self.db = db
        self.workspace_settings = None
        self.scheduled: list[str] = []

    def schedule_poll(self, reason="manual"):
        self.scheduled.append(reason)
        return asyncio.create_task(asyncio.sleep(0))


def _make_app(tmp_path, db=None):
    settings = Settings(
        _env_file=None,
        data_dir=str(tmp_path),
        admin_username="admin",
        admin_password="pw",
        session_secret="secret",
    )
    database = db if db is not None else _T52FakeDB()
    collector = _T52Collector(database)
    app = create_app(collector, settings)
    app.state.workspace_context = WorkspaceContext(
        user_id=database.user_id,
        workspace_id=database.workspace_id,
        workspace_slug="community",
        role="owner",
    )
    return app


def test_explorer_panel_has_one_sort_no_sortable_headers_or_scores():
    source = (ROOT / "app/web/templates/dashboard.html").read_text(encoding="utf-8")
    assert "<h2>Post Explorer</h2>" in source
    assert 'id="explorerSort"' in source
    assert source.count('name="sort"') == 1
    assert "sort-link" not in source
    assert "combined" not in source.lower()
    assert "success score" not in source.lower()
    assert 'name="q"' in source
    assert 'maxlength="200"' in source
    assert 'name="channels"' in source
    for metric in ("views", "reactions", "comments", "shares"):
        assert f'<option value="{metric}"' in source
    assert 'name="min_{{ metric }}"' in source
    assert 'name="max_{{ metric }}"' in source
    assert "Apply filters" in source
    assert "data-explorer-clear" in source
    assert "data-explorer-chips" in source
    assert "data-explorer-count" in source
    assert "data-explorer-rows" in source
    assert "data-explorer-error" in source
    assert 'class="explorer-filter-popover"' in source


def test_reader_dialog_skeleton_and_handoff_contract():
    source = (ROOT / "app/web/templates/dashboard.html").read_text(encoding="utf-8")
    assert 'id="postReader"' in source
    assert "data-reader" in source
    assert "data-reader-body" in source
    assert "data-reader-prev" in source
    assert "data-reader-next" in source
    assert "data-reader-explore" in source
    assert "Explore in Studio" in source
    assert "data-reader-full" in source
    assert "data-reader-error" in source
    post_source = (ROOT / "app/web/templates/post.html").read_text(encoding="utf-8")
    assert "data-explore-post" in post_source
    assert "Explore in Studio" in post_source
    assert "data-channel-id" in post_source
    assert "data-post-id" in post_source


def test_explorer_client_uses_server_state_chips_races_and_safe_render():
    source = (ROOT / "app/web/static/app.js").read_text(encoding="utf-8")
    assert "/api/explorer?" in source
    assert "explorerSequence" in source
    assert "data-explorer-chips" in source
    assert "PREFILL_STORAGE_KEY" in source
    assert "'tg-studio:prefill'" in source
    assert "sessionStorage.setItem(PREFILL_STORAGE_KEY" in source
    assert "window.location.href = '/studio'" in source
    # Excerpts render as text, never injected HTML; only the sanitized
    # server Telegram HTML enters the reader via innerHTML.
    assert "postCell.append(openButton)" in source
    assert "body.innerHTML = detail.formatted_html" in source
    assert "showModal" in source
    assert "readerDialog.addEventListener('close'" in source
    assert "readerOpener.focus()" in source
    assert "No posts match these filters" in source
    # The handoff never starts a run by itself: no agent POST from Explorer.
    assert "api/agent" not in source
    assert "setActiveElements" not in source.split("Post reader")[1].split("Standalone post page")[0]


@pytest.mark.asyncio
async def test_explorer_combines_channel_search_and_metric_ranges(tmp_path):
    app = _make_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/login", data={"username": "admin", "password": "pw"})
        both = await client.get("/api/explorer", params={"q": "markets", "min_views": "1000", "sort": "views"})
        assert both.status_code == 200
        body = both.json()
        assert body["total"] == 3
        # Views tie (2000) breaks by newer publication date, then id.
        assert [r["id"] for r in body["rows"]] == [4, 2, 1]
        assert body["page"] == 1
        scoped = await client.get("/api/explorer", params={"channels": "2", "q": "markets"})
        assert [r["id"] for r in scoped.json()["rows"]] == [4]
        ranged = await client.get("/api/explorer", params={"min_comments": "20", "max_comments": "40"})
        assert [r["id"] for r in ranged.json()["rows"]] == [3, 2]
        nothing = await client.get("/api/explorer", params={"min_views": "99999"})
        assert nothing.json()["total"] == 0
        assert nothing.json()["total_pages"] == 1


@pytest.mark.asyncio
async def test_explorer_rejects_bad_ranges_sort_and_search(tmp_path):
    app = _make_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/login", data={"username": "admin", "password": "pw"})
        bad_range = await client.get("/api/explorer", params={"min_views": "10", "max_views": "5"})
        assert bad_range.status_code == 422
        assert bad_range.json()["error"]["code"] == "invalid_range"
        bad_sort = await client.get("/api/explorer", params={"sort": "score"})
        assert bad_sort.status_code == 422
        bad_search = await client.get("/api/explorer", params={"q": "x" * 201})
        assert bad_search.status_code == 422


@pytest.mark.asyncio
async def test_reader_returns_formatted_post_discussion_and_snapshots(tmp_path):
    app = _make_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/login", data={"username": "admin", "password": "pw"})
        detail = await client.get("/api/posts/1")
        assert detail.status_code == 200
        body = detail.json()
        assert "<strong>Hello</strong>" in body["formatted_html"]
        assert body["metrics"]["views"] == 1000
        assert body["source_url"] == "https://t.me/chan_one/101"
        assert len(body["snapshots"]) == 2
        assert "collection" in body["snapshot_note"].lower()
        # Permitted discussion only: the deleted comment stays out.
        assert [c["id"] for c in body["discussion"]] == [1]
        missing = await client.get("/api/posts/999")
        assert missing.status_code == 404


@pytest.mark.asyncio
async def test_fallback_reader_page_and_explorer_scoping(tmp_path):
    app = _make_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/login", data={"username": "admin", "password": "pw"})
        page = await client.get("/post/1")
        assert page.status_code == 200
        assert "<strong>Hello</strong>" in page.text
        assert "Explore in Studio" in page.text
        assert "Collection times (UTC)" in page.text
        assert "No comments collected" not in page.text
        gone = await client.get("/post/999")
        assert gone.status_code == 404
        scoped = await client.get("/?channel=1")
        assert scoped.status_code == 200
        # The Overview channel disables the other channel checkbox.
        assert "disabled" in scoped.text
        assert 'value="1" checked' in scoped.text
        # The metric range inputs and reader dialog render for real.
        assert 'name="min_views"' in scoped.text
        assert 'name="max_shares"' in scoped.text
        assert 'id="postReader"' in scoped.text
