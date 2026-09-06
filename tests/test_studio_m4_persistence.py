from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from app.config import Settings
from app.postgres_db import PostgresDatabase
from app.studio.provenance import SourceEvidence
from app.studio.repository import StudioRepository
from app.studio.research import ResearchService
from app.studio.search import SearchQuery, SearchResponse, SearchResult


class PersistProvider:
    async def search(self, query, **kwargs):
        now = datetime.now(timezone.utc)
        result = SearchResult(
            url="https://persist.test/story",
            canonical_url="https://persist.test/story",
            title="A persisted channel topic story",
            snippet="A bounded factual source excerpt.",
            source_name="Persistence fixture",
            domain="persist.test",
            published_at=now,
            provider="fixture-search",
            query=str(query),
            fetched_at=now,
            result_index=0,
            source_id="persist-source",
            provenance={"provider": "fixture-search"},
        )
        return SearchResponse(
            query=SearchQuery(str(query)),
            results=(result,),
            provider="fixture-search",
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_m4_research_bundle_and_provider_activity_survive_reload():
    database_url = os.environ.get("M4_POSTGRES_URL")
    if not database_url:
        pytest.skip("set M4_POSTGRES_URL to run the M4 PostgreSQL persistence proof")
    db = PostgresDatabase(database_url)
    try:
        await db.init_db(admin_username="m4-persistence-admin")
        channel_id = await db.upsert_channel("@m4-persistence", "M4 Persistence", 4040)
        repository = StudioRepository(db)
        conversation = await repository.create_conversation(channel_id=channel_id)
        service = ResearchService(
            Settings(studio_search_enabled=True),
            provider=PersistProvider(),
            repository=repository,
        )
        result = await service.search(
            workspace_id=db.workspace_id,
            conversation_id=conversation["id"],
            channel_id=channel_id,
            query="topic story",
            topics=["topic"],
            channel_evidence=[
                {"post_id": 7, "message_id": 77, "link": "https://t.me/m4_persistence/77", "excerpt": "Winning topic post"}
            ],
        )
        assert result["stories"][0]["channel_evidence"][0]["link"].endswith("/77")
        assert result["stories"][0]["score_breakdown"]["weights"]["topic_relevance"] == 0.4
        assert result["sources"][0]["quality_notes"] == []
        assert result["activity"]["provider"] == "fixture-search"
        assert result["activity"]["persisted"] is True

        reloaded = ResearchService(Settings(studio_search_enabled=True), repository=StudioRepository(db))
        bundle = await reloaded.get_bundle(
            workspace_id=db.workspace_id,
            conversation_id=conversation["id"],
            channel_id=channel_id,
        )
        assert bundle is not None
        assert bundle.sources[0].source_id == "persist-source"
        assert bundle.stories[0].channel_evidence[0]["link"].endswith("/77")
        events = await repository.list_research_events(
            conversation_id=conversation["id"], channel_id=channel_id
        )
        assert events and events[0]["provider"] == "fixture-search"
    finally:
        await db.close()
