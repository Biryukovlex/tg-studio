import os
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.postgres_db import PostgresDatabase
from app.studio.analytics import analyze_posts
from app.studio.consent import configuration_fingerprint
from app.studio.repository import StudioRepository, StudioRepositoryError
from app.studio.service import StudioService


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_m3_profile_context_and_consent_are_workspace_scoped():
    database_url = os.environ.get("M3_POSTGRES_URL")
    if not database_url:
        pytest.skip("set M3_POSTGRES_URL to run the M3 PostgreSQL proof")
    db = PostgresDatabase(database_url)
    try:
        await db.init_db(admin_username="m3-integration-admin")
        channel_id = await db.upsert_channel("@m3-integration", "M3 Integration", 4004)
        now = datetime.now(timezone.utc)
        post_ids = []
        for index in range(1, 9):
            post_id = await db.upsert_post(
                channel_id,
                index,
                now - timedelta(days=index + 1),
                f"topic-{index % 3} " + ("A sufficiently long post for style evidence. " * 4),
            )
            await db.add_snapshot_if_changed(post_id, index * 100, index, index * 2, index // 2)
            post_ids.append(post_id)
        # The discussion body exists in PostgreSQL but is deliberately absent
        # from the Studio performance query and all model-facing packs.
        await db.upsert_comment(
            post_id=post_ids[0], telegram_message_id=99, discussion_chat_id=1,
            discussion_username="m3-discussion", sender_id=2, sender_name="Fixture",
            sender_username="fixture", posted_at=now, edited_at=None,
            text="PRIVATE DISCUSSION BODY MUST NOT ENTER STUDIO", media_type="",
            reactions=1, reply_to_message_id=None, sync_token="m3",
        )
        repository = StudioRepository(db)
        rows = await repository.performance_rows(channel_id)
        assert len(rows) == 8
        assert all("PRIVATE DISCUSSION BODY" not in str(row) for row in rows)
        analysis = analyze_posts(rows, channel_id, now=now, identifier="@m3-integration")
        assert analysis.eligible_post_count == 8
        service = StudioService(repository, Settings(studio_test_mode=True))
        profile_row = await service.ensure_profile(channel_id)
        assert profile_row and profile_row["current_analysis_id"]
        profile = await repository.get_profile(channel_id)
        assert profile and profile["version"] == 1
        stored_analysis = await repository.get_analysis(profile["current_analysis_id"])
        assert stored_analysis and stored_analysis["evidence_post_ids"]

        change = await repository.create_profile_change(
            {"channel_id": channel_id, "base_profile_version": 1, "proposed_topics": ["confirmed-topic"]}
        )
        assert (await repository.confirm_profile_change(change["id"]))["status"] == "confirmed"
        applied = await repository.apply_profile_change(change["id"])
        assert applied["version"] == 2
        with pytest.raises(StudioRepositoryError):
            await repository.apply_profile_change(change["id"])

        fingerprint = configuration_fingerprint(Settings(openrouter_model="model-a"))
        consent = await repository.grant_provider_consent(
            user_id=db.user_id, provider="openrouter", configuration_fingerprint=fingerprint
        )
        assert consent["allowed"] is True
        assert (await repository.get_provider_consent(provider="openrouter", configuration_fingerprint=fingerprint, user_id=db.user_id))["allowed"]
        revoked = await repository.revoke_provider_consent(user_id=db.user_id, provider="openrouter")
        assert revoked and revoked["allowed"] is False
    finally:
        await db.close()
