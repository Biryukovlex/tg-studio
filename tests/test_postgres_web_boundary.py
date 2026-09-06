from datetime import datetime, timezone
import uuid

import httpx
import pytest

from app.config import Settings
from app.db import Database
from app.web.routes import create_app


class _AsyncFacade:
    def __init__(self, database):
        self._database = database
        self.workspace_id = uuid.uuid4()
        self.user_id = uuid.uuid4()
        self.workspace_slug = "community"

    async def get_channels(self):
        return self._database.get_channels()

    async def kpis(self, channel_id=None):
        return self._database.kpis(channel_id)

    async def timeseries_totals(self, days, channel_id=None):
        return self._database.timeseries_totals(days, channel_id)

    async def latest_stats(self, **kwargs):
        return self._database.latest_stats(**kwargs)

    async def post_row(self, post_id):
        return self._database.post_row(post_id)

    async def post_history(self, post_id):
        return self._database.post_history(post_id)

    async def comments_for_post(self, post_id):
        return self._database.comments_for_post(post_id)

    async def all_comments(self, channel_id=None):
        return self._database.all_comments(channel_id)


class _Collector:
    def __init__(self, db):
        self.db = db

    async def poll_all(self, reason="test"):
        return {"reason": reason, "posts_seen": 0, "snapshots_written": 0}


@pytest.mark.asyncio
async def test_dashboard_post_comments_and_exports_use_async_db_boundary(tmp_path):
    settings = Settings(
        api_id=1,
        api_hash="hash",
        session_string="session",
        channels="@boundary",
        data_dir=str(tmp_path),
        admin_username="admin",
        admin_password="password",
        session_secret="secret",
    )
    sqlite_db = Database(settings.db_path)
    sqlite_db.init_db()
    channel_id = sqlite_db.upsert_channel("@boundary", "Boundary", 1001)
    post_id = sqlite_db.upsert_post(channel_id, 5, datetime.now(timezone.utc), "full post")
    sqlite_db.add_snapshot_if_changed(post_id, 10, 1, 2, 3)
    facade = _AsyncFacade(sqlite_db)
    app = create_app(_Collector(facade), settings)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post("/login", data={"username": "admin", "password": "password"}, follow_redirects=False)
        assert login.status_code == 303
        assert (await client.get("/")).status_code == 200
        assert (await client.get(f"/post/{post_id}")).status_code == 200
        stats = await client.get("/export.csv")
        comments = await client.get("/export-comments.csv")
        assert "full post" in stats.text
        assert "comment_message_id" in comments.text
