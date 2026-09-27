"""T41 PostgreSQL end-to-end: research → read → verified draft."""
from __future__ import annotations

import os
import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings
from app.postgres_db import PostgresDatabase
from app.studio.repository import StudioRepository
from app.studio.research import ResearchService
from app.studio.sources import SafeSourceReader

ARTICLE = (
    "Meridian Labs announced on 9 June 2026 that it raised $75 million to expand "
    "its Atlas data centers. Construction starts in September 2026."
)


def _url() -> str:
    url = os.environ.get("TEST_POSTGRES_URL", "")
    if not url:
        pytest.skip("TEST_POSTGRES_URL is required")
    return url


def _search_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={
        "query": "Meridian",
        "number_of_results": 2,
        "results": [
            {
                "url": "https://news.test/meridian",
                "title": "Meridian Labs raises funds",
                "content": "Meridian Labs funding snippet.",
                "engine": "google",
            },
            {
                "url": "https://news.test/other",
                "title": "Unrelated tablet story",
                "content": "A tablet story snippet.",
                "engine": "google",
            },
        ],
        "answers": [],
        "corrections": [],
        "infoboxes": [],
        "suggestions": [],
        "unresponsive_engines": [],
    })


def _read_handler(request: httpx.Request) -> httpx.Response:
    if str(request.url) == "https://news.test/meridian":
        return httpx.Response(
            200,
            content=f"<html><head><title>Meridian Labs raises funds</title></head><body><p>{ARTICLE}</p></body></html>".encode(),
            headers={"content-type": "text/html"},
        )
    return httpx.Response(404, content=b"missing")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_research_read_draft_verifies_claim_on_postgres():
    from app.studio.search import SearXNGSearchProvider

    base = _url()
    slug = f"t41-{uuid.uuid4().hex[:8]}"
    db = PostgresDatabase(base, workspace_slug=slug)
    try:
        await db.init_db(admin_username="t41-admin")
        channel_id = await db.upsert_channel(f"@t41_{uuid.uuid4().hex[:6]}", "T41", 7401)
        settings = Settings(
            _env_file=None, studio_test_mode=True,
            studio_search_enabled=True, studio_search_base_url="http://searxng.test",
        )
        repository = StudioRepository(db)
        conversation = await repository.create_conversation(channel_id=channel_id, title="Verification")
        provider = SearXNGSearchProvider(settings, transport=httpx.MockTransport(_search_handler))
        reader = SafeSourceReader(
            settings, transport=httpx.MockTransport(_read_handler), allow_private_for_tests=True,
        )
        service = ResearchService(settings, provider=provider, reader=reader, repository=repository)
        searched = await service.search(
            workspace_id=repository.workspace_id, conversation_id=conversation["id"],
            channel_id=channel_id, query="Meridian funding",
        )
        assert len(searched["sources"]) == 2
        read = await service.read_sources(
            workspace_id=repository.workspace_id, conversation_id=conversation["id"],
            channel_id=channel_id, urls=["https://news.test/meridian"],
        )
        assert read["sources"][0]["source_id"]

        from app.studio.provenance import verify_claim_support
        from app.studio.research import ResearchService as _RS

        verifier = _RS(settings, repository=repository)
        bundle = await verifier.get_bundle(
            workspace_id=repository.workspace_id, conversation_id=conversation["id"], channel_id=channel_id,
        )
        assert bundle is not None
        by_id = {source.source_id: source for source in bundle.sources}
        meridian_id = next(sid for sid, source in by_id.items() if "meridian" in source.url)
        other_id = next(sid for sid, source in by_id.items() if "other" in source.url)

        accepted, failures = verify_claim_support(by_id, [{
            "claim": "Meridian Labs raised $75 million in 2026.",
            "source_ids": [meridian_id],
            "passage": "Meridian Labs announced on 9 June 2026 that it raised $75 million",
        }])
        assert failures == []
        assert accepted[0]["verified"] is True

        accepted, failures = verify_claim_support(by_id, [{
            "claim": "Meridian Labs raised $75 million in 2026.",
            "source_ids": [other_id],
            "passage": "A tablet story snippet.",
        }])
        assert accepted == []
        assert failures and failures[0].code in ("snippet_only", "unsupported_detail")

        draft = await repository.create_draft(
            conversation_id=conversation["id"], channel_id=channel_id,
            payload={
                "body": "Meridian Labs raised $75 million in 2026.",
                "source_ids": [meridian_id],
                "claim_support": [{
                    "claim": "Meridian Labs raised $75 million in 2026.",
                    "source_ids": [meridian_id],
                    "passage": "Meridian Labs announced on 9 June 2026 that it raised $75 million",
                    "verified": True,
                }],
            },
        )
        assert draft["claim_support"][0]["verified"] is True

        # Reload through a fresh repository: verification state survives.
        fresh = StudioRepository(db)
        reloaded = await fresh.get_current_draft(conversation_id=conversation["id"], channel_id=channel_id)
        assert reloaded is not None
        assert reloaded["claim_support"][0]["verified"] is True
        assert reloaded["claim_support"][0]["passage"].startswith("Meridian Labs announced on 9 June 2026")
    finally:
        engine = create_async_engine(base)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("DELETE FROM workspaces WHERE slug=:slug"), {"slug": slug})
                await conn.commit()
        finally:
            await engine.dispose()
        await db.close()
