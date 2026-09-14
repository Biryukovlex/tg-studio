from datetime import datetime, timezone
import os
import uuid

import httpx
import pytest
from sqlalchemy import text

from app.config import Settings
from app.postgres_db import PostgresDatabase
from app.web.routes import create_app


pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"), reason="set TEST_POSTGRES_URL to run the PostgreSQL proof"
)


class _Collector:
    def __init__(self, db):
        self.db = db

    async def poll_all(self, reason="test"):
        return {"reason": reason, "posts_seen": 0, "snapshots_written": 0}


@pytest.mark.asyncio
async def test_postgres_overview_excludes_deactivated_channels(tmp_path):
    slug = f"t24-{uuid.uuid4().hex[:10]}"
    db = PostgresDatabase(os.environ["TEST_POSTGRES_URL"], workspace_slug=slug)
    await db.init_db(admin_username=f"{slug}-admin")
    try:
        active = await db.add_channel(f"@{slug}-active")
        retired = await db.add_channel(f"@{slug}-retired")
        now = datetime.now(timezone.utc)
        active_post = await db.upsert_post(active, 1, now, "active post")
        retired_post = await db.upsert_post(retired, 2, now, "retired post")
        await db.add_snapshot_if_changed(active_post, 10, 2, 3, 4)
        await db.add_snapshot_if_changed(retired_post, 100, 20, 30, 40)
        for post_id, message_id in ((active_post, 11), (retired_post, 22)):
            await db.upsert_comment(
                post_id=post_id,
                telegram_message_id=message_id,
                discussion_chat_id=9000,
                discussion_username="fixture-comments",
                sender_id=message_id,
                sender_name="Fixture commenter",
                sender_username="fixture",
                posted_at=now,
                edited_at=None,
                text="A fixture comment",
                media_type="",
                reactions=1,
                reply_to_message_id=None,
                sync_token=f"t24-{message_id}",
            )
        assert await db.deactivate_channel(retired)

        kpis = await db.kpis()
        assert int(kpis["posts"]) == 1
        assert int(kpis["views"]) == 10
        assert int(kpis["collected_comments"]) == 1
        assert len(await db.latest_stats()) == 1
        assert len(await db.all_comments()) == 1
        assert int((await db.history_diagnostic())["total_posts"]) == 1
        assert (await db.timeseries_totals(days=None))["views"][-1] == 10

        # Explicit archive inspection still includes an inactive channel.
        assert int((await db.kpis(retired))["posts"]) == 1
        assert len(await db.latest_stats(retired)) == 1

        settings = Settings(
            _env_file=None,
            database_url=os.environ["TEST_POSTGRES_URL"],
            channels="",
            data_dir=str(tmp_path),
            admin_username=f"{slug}-admin",
            admin_password="password",
            session_secret="t24-test-secret",
        )
        app = create_app(_Collector(db), settings)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            login = await client.post(
                "/login",
                data={"username": f"{slug}-admin", "password": "password"},
                follow_redirects=False,
            )
            assert login.status_code == 303
            dashboard = await client.get("/")
            assert dashboard.status_code == 200
            assert "1 posts tracked" in dashboard.text
            exported = await client.get("/export.csv")
            assert exported.status_code == 200
            assert len(exported.text.splitlines()) == 2
    finally:
        async with db.sessions.session() as session:
            await session.execute(text("DELETE FROM workspaces WHERE slug=:slug"), {"slug": slug})
            await session.commit()
        await db.close()
