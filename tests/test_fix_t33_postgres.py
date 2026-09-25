"""T33 PostgreSQL tombstone proof: bounded retirement, filters, and un-delete."""
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.postgres_db import PostgresDatabase


def _url() -> str:
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("set TEST_POSTGRES_URL to run the T33 PostgreSQL proof")
    return url


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retired_post_stays_accessible_with_deleted_badge(app, client, settings, channel_id):
    db = app.state.db
    post_id = await db.upsert_post(
        channel_id, 43, datetime.now(timezone.utc), "A post removed later in Telegram"
    )
    await db.add_snapshot_if_changed(post_id, 5, 0, 0, 0)
    await db.mark_unseen_posts_deleted(channel_id, [42], min_message_id=42)

    login = await client.post(
        "/login",
        data={"username": settings.admin_username, "password": settings.admin_password},
    )
    assert login.status_code == 303
    detail = await client.get(f"/post/{post_id}")
    assert detail.status_code == 200
    assert "Deleted in Telegram" in detail.text
    assert "A post removed later in Telegram" in detail.text
    assert (await db.kpis(channel_id))["posts"] == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_tombstone_bounds_filters_and_undelete():
    base = _url()
    slug = f"t33-{uuid.uuid4().hex[:8]}"
    db = PostgresDatabase(base, workspace_slug=slug)
    try:
        await db.init_db(admin_username="t33-admin")
        identifier = f"@t33_{uuid.uuid4().hex[:6]}"
        channel_id = await db.upsert_channel(identifier, "T33", 7033)
        now = datetime.now(timezone.utc)
        for mid in (1, 2, 3):
            post_id = await db.upsert_post(channel_id, mid, now, f"post {mid}")
            await db.add_snapshot_if_changed(post_id, mid * 10, 0, 0, 0)

        # Bounded scan sees 1 and 3: post 2 is inside the window and retires.
        retired = await db.mark_unseen_posts_deleted(channel_id, [1, 3], since=None, min_message_id=1)
        assert retired == 1

        assert (await db.kpis())["posts"] == 2
        assert len(await db.latest_stats()) == 2
        assert len(await db.all_comments()) == 0
        series = await db.timeseries_totals(days=None)
        assert series["views"][-1] == 40

        # Posts below the message bound are out of the proven window and stay.
        engine = create_async_engine(base)
        try:
            async with engine.connect() as conn:
                ws = await conn.execute(text("SELECT id FROM workspaces WHERE slug=:slug"), {"slug": slug})
                workspace_id = ws.scalar_one()
                old_id = await conn.execute(
                    text(
                        "INSERT INTO posts(workspace_id, channel_id, message_id, posted_at, text,"
                        " formatting_entities, is_deleted, created_at)"
                        " VALUES (:ws, :ch, 0, :posted, 'old', '[]', false, now()) RETURNING id"
                    ),
                    {"ws": workspace_id, "ch": channel_id, "posted": now - timedelta(days=60)},
                )
                old_post_id = int(old_id.scalar_one())
                await conn.commit()
        finally:
            await engine.dispose()

        retired = await db.mark_unseen_posts_deleted(channel_id, [1, 3], since=None, min_message_id=1)
        assert retired == 0
        engine = create_async_engine(base)
        try:
            async with engine.connect() as conn:
                flag = await conn.execute(text("SELECT is_deleted FROM posts WHERE id=:id"), {"id": old_post_id})
                assert flag.scalar_one() is False
        finally:
            await engine.dispose()

        # A date bound protects older posts even when their id is in range.
        retired = await db.mark_unseen_posts_deleted(channel_id, [1, 3], since=now - timedelta(days=30), min_message_id=0)
        assert retired == 0

        # Re-seeing a retired post clears the flag (upsert path plus explicit clear).
        await db.upsert_post(channel_id, 2, now, "post 2 again")
        await db.mark_unseen_posts_deleted(channel_id, [0, 1, 2, 3], since=None, min_message_id=0)
        assert (await db.kpis())["posts"] == 4
    finally:
        engine = create_async_engine(base)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("DELETE FROM workspaces WHERE slug=:slug"), {"slug": slug})
                await conn.commit()
        finally:
            await engine.dispose()
        await db.close()
