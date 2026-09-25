import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app.postgres_db import PostgresDatabase

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"), reason="TEST_POSTGRES_URL is required"
)


async def _comment(db: PostgresDatabase, post_id: int, message_id: int) -> None:
    now = datetime.now(timezone.utc)
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


@pytest.mark.asyncio
async def test_overview_queries_exclude_deactivated_channels():
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
        await _comment(db, active_post, 11)
        await _comment(db, retired_post, 22)

        assert await db.deactivate_channel(retired)

        kpis = await db.kpis()
        assert int(kpis["posts"]) == 1
        assert int(kpis["views"]) == 10
        assert int(kpis["comments"]) == 2
        assert int(kpis["collected_comments"]) == 1
        assert len(await db.latest_stats()) == 1
        assert len(await db.all_comments()) == 1
        assert int((await db.history_diagnostic())["total_posts"]) == 1

        series = await db.timeseries_totals(days=None)
        assert series["views"][-1] == 10
        assert series["posts_per_day"][-1] == 1

        # A specifically selected channel keeps the historical behaviour, even if
        # the channel is inactive and retained for archive inspection.
        assert int((await db.kpis(retired))["posts"]) == 1
        assert int((await db.kpis(retired))["views"]) == 100
        assert len(await db.latest_stats(retired)) == 1
        assert len(await db.all_comments(retired)) == 1
        assert int((await db.history_diagnostic(retired))["total_posts"]) == 1
    finally:
        async with db.sessions.session() as session:
            await session.execute(text("DELETE FROM workspaces WHERE slug=:slug"), {"slug": slug})
            await session.commit()
        await db.close()
