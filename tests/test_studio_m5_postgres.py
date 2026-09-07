from __future__ import annotations

import os
import uuid

import pytest

from app.postgres_db import PostgresDatabase
from app.studio.drafts import DraftConflictError, DraftValidationError
from app.studio.repository import StudioRepository


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_draft_versions_survive_repository_reload_and_keep_scope():
    database_url = os.environ.get("M5_POSTGRES_URL")
    if not database_url:
        pytest.skip("set M5_POSTGRES_URL to run the M5 PostgreSQL persistence proof")
    db = PostgresDatabase(database_url, workspace_slug="m5-persistence")
    try:
        await db.init_db(admin_username="m5-persistence-admin")
        channel_id = await db.upsert_channel("@m5-persistence", "M5 Persistence", 5050)
        repository = StudioRepository(db)
        conversation = await repository.create_conversation(channel_id=channel_id)
        source_id = f"m5-source-{uuid.uuid4().hex[:12]}"
        await repository.persist_research_bundle(
            conversation_id=conversation["id"],
            channel_id=channel_id,
            bundle={"sources": [{"source_id": source_id, "url": "https://news.test/m5"}], "stories": []},
        )
        draft = await repository.create_draft(
            conversation_id=conversation["id"],
            channel_id=channel_id,
            payload={
                "body": "Generated first 🙂",
                "working_title": "M5 persistence",
                "source_ids": [source_id],
                "claim_support": [{"claim": "Generated first 🙂", "source_ids": [source_id]}],
            },
        )
        edited = await repository.save_draft(
            draft_id=draft["id"],
            payload={"body": "Owner edit\n\nwith a paragraph break."},
            expected_revision=draft["revision"],
            new_version=True,
        )

        reloaded = StudioRepository(db)
        current = await reloaded.get_current_draft(conversation_id=conversation["id"], channel_id=channel_id)
        assert current and current["body"] == edited["body"]
        assert current["revision"] == 2
        generated = await reloaded.revise_draft(
            draft_id=draft["id"],
            payload={"body": "Generated candidate", "source_ids": [source_id]},
            instruction="Try a shorter angle.",
        )
        assert generated["body"] == "Generated candidate"
        assert generated["current_version"] == 3
        versions = await reloaded.list_draft_versions(
            draft_id=draft["id"],
            conversation_id=conversation["id"],
            channel_id=channel_id,
        )
        assert [item["origin"] for item in versions] == ["generated", "user_edit", "regenerated"]
        assert versions[-1]["body"] == "Generated candidate"

        with pytest.raises(DraftConflictError):
            await reloaded.save_draft(
                draft_id=draft["id"],
                payload={"body": "stale"},
                expected_revision=1,
            )
        copied = await reloaded.mark_draft_copied(draft_id=draft["id"])
        assert copied["body"] == "Generated candidate"
        assert copied["copied_at"]
    finally:
        await db.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_draft_rejects_analysis_from_another_channel():
    database_url = os.environ.get("M5_POSTGRES_URL")
    if not database_url:
        pytest.skip("set M5_POSTGRES_URL to run the M5 PostgreSQL scope proof")
    db = PostgresDatabase(database_url, workspace_slug="m5-scope")
    try:
        await db.init_db(admin_username="m5-scope-admin")
        first_channel = await db.upsert_channel("@m5-scope-one", "M5 Scope One", 5051)
        second_channel = await db.upsert_channel("@m5-scope-two", "M5 Scope Two", 5052)
        repository = StudioRepository(db)
        repository_first = await repository.create_conversation(channel_id=first_channel)
        conversation_second = await repository.create_conversation(channel_id=second_channel)
        analysis = await repository.create_analysis(
            {
                "channel_id": first_channel,
                "analysis_start": None,
                "analysis_end": None,
                "input_hash": f"m5-scope-{uuid.uuid4().hex}",
            }
        )
        with pytest.raises(DraftValidationError, match="not part of this conversation"):
            await repository.create_draft(
                conversation_id=conversation_second["id"],
                channel_id=second_channel,
                payload={"body": "Cross-channel analysis", "analysis_id": str(analysis["id"]), "creative": True},
            )
        assert repository_first["channel_id"] == first_channel
    finally:
        await db.close()
