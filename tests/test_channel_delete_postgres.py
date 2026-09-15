"""PostgreSQL proof that deleting a channel purges its complete data graph."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest

from app.postgres_db import PostgresDatabase
from app.studio.repository import StudioRepository


def _url() -> str:
    value = os.environ.get("TEST_POSTGRES_URL") or os.environ.get("M6_POSTGRES_URL")
    if not value:
        pytest.skip("set TEST_POSTGRES_URL to run the channel deletion PostgreSQL proof")
    return value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_channel_cascades_archive_statistics_and_studio_workspace():
    db = PostgresDatabase(_url(), workspace_slug=f"delete-{uuid.uuid4().hex[:10]}")
    try:
        await db.init_db(admin_username=f"delete-{uuid.uuid4().hex[:8]}")
        target = await db.upsert_channel(f"@delete_{uuid.uuid4().hex[:8]}", "Delete me", 7001)
        survivor = await db.upsert_channel(f"@keep_{uuid.uuid4().hex[:8]}", "Keep me", 7002)
        post_id = await db.upsert_post(target, 1, datetime.now(timezone.utc), "Synthetic post")
        await db.add_snapshot_if_changed(post_id, 120, 1, 4, 2)
        await db.upsert_comment(
            post_id=post_id,
            telegram_message_id=10,
            posted_at=datetime.now(timezone.utc),
            text="Synthetic comment",
            sync_token="synthetic-delete-proof",
        )

        repository = StudioRepository(db)
        conversation = await repository.create_conversation(channel_id=target, title="Synthetic conversation")
        message = await repository.append_message(
            conversation_id=conversation["id"], role="user", content="Create a synthetic draft"
        )
        run = await repository.create_run(
            conversation_id=conversation["id"],
            user_message_id=message["id"],
            requested_model="synthetic/model",
        )
        await repository.append_event(
            run["id"], event_type="TOOL_CALL_RESULT", safe_payload={"tool_name": "synthetic"}, result_content="synthetic"
        )
        await repository.create_draft(
            conversation_id=conversation["id"],
            channel_id=target,
            payload={"body": "Synthetic draft", "working_title": "Synthetic", "creative": True},
        )
        await repository.upsert_profile_text(
            {
                "channel_id": target,
                "expected_version": 0,
                "topics_text": "Synthetic topic — deletion proof",
                "editorial_text": "Synthetic rule.",
                "style_text": "Synthetic style.",
                "built_from_posts": 1,
            }
        )

        summary = (await db.channel_data_summaries())[target]
        assert summary == {"posts": 1, "comments": 1, "conversations": 1, "drafts": 1}
        assert await db.deactivate_channel(target)
        settings_channels = await db.get_channels_for_settings()
        assert any(int(row["id"]) == target and row["active"] is False for row in settings_channels)

        with pytest.raises(ValueError):
            await db.delete_channel(target, confirmation="wrong")
        assert any(int(row["id"]) == target for row in await db.get_channels_for_settings())

        deleted = await db.delete_channel(target, confirmation=(await db._execute(
            "SELECT identifier FROM channels WHERE workspace_id=:workspace_id AND id=:channel_id",
            {"channel_id": target},
        )).scalar_one())
        assert deleted is not None and deleted["posts"] == 1 and deleted["conversations"] == 1
        assert [int(row["id"]) for row in await db.get_channels()] == [survivor]

        for table in (
            "posts",
            "snapshots",
            "comments",
            "collection_jobs",
            "studio_conversations",
            "studio_messages",
            "studio_agent_runs",
            "studio_run_events",
            "studio_analyses",
            "studio_profiles",
            "studio_profile_changes",
            "studio_sources",
            "studio_story_clusters",
            "studio_research_events",
            "studio_drafts",
            "studio_draft_versions",
        ):
            remaining = (
                await db._execute(f'SELECT COUNT(*) FROM "{table}" WHERE workspace_id=:workspace_id')
            ).scalar_one()
            assert remaining == 0, table
    finally:
        await db.close()
