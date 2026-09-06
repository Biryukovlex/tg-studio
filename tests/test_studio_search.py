from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.studio.search import (
    DegradedSearchProvider,
    SearchCache,
    SearchQuery,
    SearXNGSearchProvider,
    build_search_provider,
    canonicalize_url,
)


def test_canonicalize_url_removes_tracking_and_credentials():
    assert canonicalize_url("HTTPS://Example.COM:443/story/?utm_source=x&b=2#comments") == "https://example.com/story?b=2"
    assert canonicalize_url("https://user:password@example.com/story") == ""
    assert canonicalize_url("file:///tmp/article") == ""


@pytest.mark.asyncio
async def test_searxng_normalizes_deduplicates_and_caches_metadata_only():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == "/search"
        assert request.url.params["format"] == "json"
        assert "news" in request.url.params["categories"]
        assert request.url.params["time_range"] == "week"
        assert "site:example.com" in request.url.params["q"]
        return httpx.Response(
            200,
            json={
                "private_provider_payload": "must not leak",
                "results": [
                    {
                        "url": "https://Example.com/story/?utm_medium=x",
                        "title": "  A story  ",
                        "content": "A bounded summary.",
                        "engine": "fixture",
                        "publishedDate": "2026-09-01T12:00:00Z",
                    },
                    {
                        "url": "https://example.com/story#duplicate",
                        "title": "duplicate",
                        "content": "duplicate",
                    },
                ],
            },
        )

    settings = Settings(
        studio_search_enabled=True,
        studio_search_base_url="http://private-search:8080",
        studio_search_cache_ttl_seconds=60,
    )
    provider = SearXNGSearchProvider(settings, transport=httpx.MockTransport(handler), cache=SearchCache(ttl_seconds=60))
    query = SearchQuery("fresh story", recency_days=7, domains=("example.com",), limit=5)
    first = await provider.search(query)
    second = await provider.search(query)

    assert calls == 1
    assert first.degraded is False
    assert len(first.results) == 1
    assert first.results[0].canonical_url == "https://example.com/story"
    assert first.results[0].source_id
    assert first.results[0].provenance["provider"] == "searxng"
    assert "private_provider_payload" not in str(first.model_dump())
    assert second.cache_hit is True
    assert second.results[0].canonical_url == first.results[0].canonical_url


@pytest.mark.asyncio
async def test_provider_retries_transient_failure_with_bounded_attempts():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, text="internal details")
        return httpx.Response(200, json={"results": [{"url": "https://example.test/a", "title": "A", "content": "B"}]})

    settings = Settings(studio_search_enabled=True, studio_search_base_url="http://private-search:8080", studio_search_retries=1)
    provider = SearXNGSearchProvider(settings, transport=httpx.MockTransport(handler))
    response = await provider.search("retry me")
    assert calls == 2
    assert response.degraded is False
    assert response.results[0].domain == "example.test"


@pytest.mark.asyncio
async def test_disabled_and_unsupported_providers_are_explicitly_degraded():
    disabled = build_search_provider(Settings(studio_search_enabled=False))
    response = await disabled.search("topic")
    assert isinstance(disabled, DegradedSearchProvider)
    assert response.degraded is True
    assert response.results == ()

    unsupported = build_search_provider(Settings(studio_search_enabled=True, studio_search_provider="other"))
    response = await unsupported.search("topic")
    assert response.degraded is True
    assert "supported" in response.warnings[0]


def test_search_query_and_result_bounds_are_server_side():
    query = SearchQuery("x" * 5_000, categories=("news", "not-a-category"), domains=tuple("example.com" for _ in range(20)), limit=999)
    # The provider normalizes on execution; no browser-supplied limit can
    # exceed the configured maximum.
    settings = Settings(studio_search_enabled=True, studio_search_base_url="", studio_search_max_results=3)
    provider = SearXNGSearchProvider(settings)
    import asyncio

    response = asyncio.run(provider.search(query))
    assert response.degraded is True  # no network/configured test service
    assert len(response.query.text) == 500
    assert response.query.limit == 3
    assert len(response.query.domains) <= 8


@pytest.mark.asyncio
async def test_category_and_domain_inputs_are_normalized_into_provider_request():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"results": []})

    provider = SearXNGSearchProvider(
        Settings(studio_search_enabled=True, studio_search_base_url="http://private-search:8080"),
        transport=httpx.MockTransport(handler),
    )
    response = await provider.search(
        "topic",
        categories=("news", "invalid"),
        domains=("Example.com",),
        language="en",
        recency_days=7,
    )
    assert response.degraded is False
    assert seen["categories"] == "news"
    assert "site:example.com" in seen["q"]


@pytest.mark.asyncio
async def test_precise_query_leaves_candidate_selection_to_agent():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["engines"] == "yandex,github,arxiv,wikipedia"
        return httpx.Response(
            200,
            json={
                "results": [
                    {"url": "https://openai.com/", "title": "OpenAI", "content": "Research and deployment."},
                    {
                        "url": "https://example.test/background-mode",
                        "title": "Responses API background mode guide",
                        "content": "OpenAI Responses API can run a task in background mode.",
                    },
                ]
            },
        )

    provider = SearXNGSearchProvider(
        Settings(studio_search_enabled=True, studio_search_base_url="http://private-search:8080"),
        transport=httpx.MockTransport(handler),
    )
    response = await provider.search("OpenAI Responses API background mode")

    assert [result.url for result in response.results] == ["https://openai.com/", "https://example.test/background-mode"]
    assert response.results[1].provenance["matched_query_terms"] >= 2


@pytest.mark.asyncio
async def test_agent_engine_choice_and_blocked_domains_are_enforced():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://sostav.ru/blogs/1",
                        "title": "Open source agent release",
                        "content": "A new open source agent release.",
                    },
                    {
                        "url": "https://github.com/example/agent/releases",
                        "title": "Open source agent release",
                        "content": "Official repository release notes for the agent.",
                    },
                ]
            },
        )

    provider = SearXNGSearchProvider(
        Settings(studio_search_enabled=True, studio_search_base_url="http://private-search:8080"),
        transport=httpx.MockTransport(handler),
    )
    response = await provider.search(
        "open source agent release",
        engines=("github", "arxiv", "not-allowed"),
        excluded_domains=("sostav.ru",),
    )

    assert seen["engines"] == "github,arxiv"
    assert [result.domain for result in response.results] == ["github.com"]
    assert any("low-trust" in warning for warning in response.warnings)


@pytest.mark.asyncio
async def test_concept_collisions_are_agent_decisions_and_metadata_date_is_preserved():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://cars.test/q4-model",
                        "title": "Audi sharpens Q4 model pricing",
                        "content": "The latest electric car model arrives next year.",
                    },
                    {
                        "url": "https://research.test/open-model",
                        "title": "Open-weight AI model released",
                        "content": "A new open-weight AI model is available for agents.",
                        "metadata": "9/3/2026 | Research Lab",
                    },
                ]
            },
        )

    provider = SearXNGSearchProvider(
        Settings(studio_search_enabled=True, studio_search_base_url="http://private-search:8080"),
        transport=httpx.MockTransport(handler),
    )
    response = await provider.search("open weights AI model launch")

    assert [result.domain for result in response.results] == ["cars.test", "research.test"]
    assert response.results[1].published_at.isoformat().startswith("2026-09-03")
