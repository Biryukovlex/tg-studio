"""PostgreSQL proof that Overview chart dates come from posts, not syncs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import uuid

import pytest
from sqlalchemy import text

from app.postgres_db import PostgresDatabase


pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"), reason="set TEST_POSTGRES_URL to run the PostgreSQL proof"
)


@pytest.mark.asyncio
async def test_postgres_overview_chart_uses_publication_dates():
    slug = f"post-dates-{uuid.uuid4().hex[:10]}"
    db = PostgresDatabase(os.environ["TEST_POSTGRES_URL"], workspace_slug=slug)
    await db.init_db(admin_username=f"{slug}-admin")
    try:
        channel_id = await db.add_channel(f"@{slug}")
        today = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
        older_day = today - timedelta(days=4)
        newer_day = today - timedelta(days=2)
        older_post = await db.upsert_post(channel_id, 1, older_day, "older post")
        newer_post = await db.upsert_post(channel_id, 2, newer_day, "newer post")
        await db.add_snapshot_if_changed(older_post, 11, 1, 2, 3)
        await db.add_snapshot_if_changed(newer_post, 22, 4, 5, 6)

        series = await db.timeseries_totals(days=None, channel_id=channel_id)
        older_index = series["days"].index(older_day.strftime("%Y-%m-%d"))
        newer_index = series["days"].index(newer_day.strftime("%Y-%m-%d"))
        assert series["views"][older_index] == 11
        assert series["views"][older_index + 1] == 0
        assert series["views"][newer_index] == 22
        assert series["posts_per_day"][older_index] == 1
        assert series["posts_per_day"][newer_index] == 1
    finally:
        async with db.sessions.session() as session:
            await session.execute(text("DELETE FROM workspaces WHERE slug=:slug"), {"slug": slug})
            await session.commit()
        await db.close()
