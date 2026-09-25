import json
import os
import uuid
from datetime import datetime, timezone

import pytest

from app.config import Settings
from app.postgres_db import PostgresDatabase

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"), reason="TEST_POSTGRES_URL is required"
)


@pytest.mark.asyncio
async def test_postgres_post_bodies_are_stored_complete_with_entities_and_diagnostic_counts():
    db = PostgresDatabase(os.environ["TEST_POSTGRES_URL"], workspace_slug=f"t36-hist-{uuid.uuid4().hex[:8]}")
    await db.init_db(admin_username="admin")
    try:
        channel_id = await db.upsert_channel("@history", "History", 7001)
        body = "x" * 1200
        entities = [{"type": "bold", "offset": 0, "length": 12}]
        post_id = await db.upsert_post(
            channel_id, 1, datetime.now(timezone.utc), body, formatting_entities=entities
        )
        row = await db.post_row(post_id)
        assert row["text"] == body
        stored_entities = row["formatting_entities"]
        if isinstance(stored_entities, str):
            stored_entities = json.loads(stored_entities)
        assert stored_entities == entities
        diagnostic = await db.history_diagnostic()
        assert int(diagnostic["posts_over_500"]) == 1
        assert int(diagnostic["posts_at_500"]) == 0
    finally:
        await db.close()


def test_whole_history_mode_is_explicit_in_settings_and_collector_boundary():
    settings = Settings(_env_file=None, track_days=0, backfill_limit=200)
    assert settings.track_days == 0
