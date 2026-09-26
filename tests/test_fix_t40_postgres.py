"""T40 PostgreSQL acceptance: partial search status persists and reloads."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.studio.repository import StudioRepository
from app.studio.research import ResearchService
from app.studio.search import SearchQuery, SearchResponse, SearchResult


def _response(query_text: str, *, outcome: str, failures=()) -> SearchResponse:
    now = datetime.now(timezone.utc)
    return SearchResponse(
        query=SearchQuery(text=query_text),
        results=(
            SearchResult(
                url="https://news.test/usable", canonical_url="https://news.test/usable",
                title="Usable story", snippet="Usable snippet.", source_name="News",
                domain="news.test", published_at=None, provider="searxng",
                query=query_text, fetched_at=now, result_index=0, source_id="usable-1",
            ),
        ),
        outcome=outcome,
        engine_failures=tuple(failures),
    )


class _PartialProvider:
    async def search(self, query, **kwargs):
        return _response(
            str(query),
            outcome="partial",
            failures=({"engine": "bing", "code": "engine_rate_limited", "reason": "429", "relevant": True},),
        )


@pytest.mark.asyncio
async def test_partial_status_persists_and_reloads_with_usable_sources(app, settings, channel_id):
    settings.studio_test_mode = True
    repository = app.state.studio_repository
    conversation = await repository.create_conversation(channel_id=channel_id, title="Partial search")
    service = ResearchService(settings, provider=_PartialProvider(), repository=repository)
    bundle = await service.search(
        workspace_id=repository.workspace_id,
        conversation_id=conversation["id"],
        channel_id=channel_id,
        query="channel topic",
    )
    assert bundle["search_outcome"] == "partial"
    assert bundle["degraded"] is True
    assert bundle["failed_engines"] == ["bing"]
    assert len(bundle["sources"]) == 1
    assert "partial" in bundle["status_summary"]
    assert "not unavailable" in bundle["status_summary"]

    events = await repository.list_research_events(
        conversation_id=conversation["id"], channel_id=channel_id
    )
    search_events = [event for event in events if event.get("event_type") == "search"]
    assert search_events
    metadata = search_events[0].get("metadata_json") or {}
    if isinstance(metadata, str):
        import json as _json

        metadata = _json.loads(metadata)
    assert metadata.get("search_outcome") == "partial"
    assert metadata.get("failed_engines") == ["bing"]

    # Reload through a fresh repository instance on the same workspace.
    reloaded = StudioRepository(app.state.db)
    fresh_events = await reloaded.list_research_events(
        conversation_id=conversation["id"], channel_id=channel_id
    )
    fresh_search = [event for event in fresh_events if event.get("event_type") == "search"]
    assert fresh_search
    fresh_metadata = fresh_search[0].get("metadata_json") or {}
    if isinstance(fresh_metadata, str):
        import json as _json

        fresh_metadata = _json.loads(fresh_metadata)
    assert fresh_metadata.get("search_outcome") == "partial"
