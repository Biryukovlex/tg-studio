"""T32 dashboard link, pagination, and refresh acceptance tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.config import Settings
from app.db import Database
from app.web.links import normalize_channel_identifier, telegram_message_link
from app.web.routes import create_app


def test_channel_identifiers_and_message_links_are_canonical():
    assert normalize_channel_identifier("https://t.me/news_room") == "@news_room"
    assert normalize_channel_identifier("t.me/news_room") == "@news_room"
    assert normalize_channel_identifier("-1001234567890") == "-1001234567890"
    assert telegram_message_link(identifier="t.me/news_room", message_id=12) == "https://t.me/news_room/12"
    assert telegram_message_link(identifier="-1001234567890", chat_id=-1001234567890, message_id=12) == "https://t.me/c/1234567890/12"
    assert telegram_message_link(chat_id=-1001234567890, message_id=12) == "https://t.me/c/1234567890/12"
    assert telegram_message_link(identifier="@missing", message_id=12, fallback="#") == "https://t.me/missing/12"
    assert telegram_message_link(identifier="", chat_id=None, message_id=12) == "#"


class _RefreshCollector:
    client = object()

    def __init__(self, db):
        self.db = db
        self.scheduled: list[str] = []

    def schedule_poll(self, reason="manual"):
        self.scheduled.append(reason)
        return asyncio.create_task(asyncio.sleep(0))


@pytest.mark.asyncio
async def test_dashboard_paginates_posts_and_uses_supervised_refresh(tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=str(tmp_path),
        admin_username="admin",
        admin_password="pw",
        session_secret="secret",
    )
    db = Database(settings.db_path)
    db.init_db()
    channel_id = db.upsert_channel("@page_channel", "Page channel", 12345)
    now = datetime.now(timezone.utc)
    for index in range(250):
        post_id = db.upsert_post(
            channel_id,
            index + 1,
            now - timedelta(minutes=index),
            f"page post {index + 1}",
        )
        db.add_snapshot_if_changed(post_id, views=index + 1, comments=0, reactions=0, shares=0)
    collector = _RefreshCollector(db)
    app = create_app(collector, settings)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post("/login", data={"username": "admin", "password": "pw"})
        assert login.status_code == 303
        first = await client.get("/")
        assert first.status_code == 200
        assert "page post 1" in first.text
        assert "page post 101" not in first.text
        assert "Showing 1–100 of 250" in first.text
        second = await client.get("/?page=2")
        assert second.status_code == 200
        assert "page post 101" in second.text
        assert "page post 1<" not in second.text
        assert "Showing 101–200 of 250" in second.text
        third = await client.get("/?page=3")
        assert third.status_code == 200
        assert "page post 201" in third.text
        assert "page post 101" not in third.text
        assert "Showing 201–250 of 250" in third.text
        assert 'aria-label="Chart window"' in first.text
        refresh = await client.post("/refresh", follow_redirects=False)
        assert refresh.status_code == 303
        assert collector.scheduled == ["web"]


@pytest.mark.asyncio
async def test_csv_channel_cells_are_formula_safe(tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=str(tmp_path),
        admin_username="admin",
        admin_password="pw",
        session_secret="secret",
    )
    db = Database(settings.db_path)
    db.init_db()
    channel_id = db.upsert_channel("=HYPERLINK(\"https://evil.test\")", "Formula", 42)
    post_id = db.upsert_post(channel_id, 1, datetime.now(timezone.utc), "safe")
    db.add_snapshot_if_changed(post_id, 1, 0, 0, 0)
    app = create_app(_RefreshCollector(db), settings)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/login", data={"username": "admin", "password": "pw"})
        response = await client.get("/export.csv")
    assert response.status_code == 200
    assert "'=HYPERLINK" in response.text
