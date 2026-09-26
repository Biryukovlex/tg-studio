"""T40 search-partial-status acceptance tests (synthetic SearXNG payloads)."""
from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.studio.research import ResearchService
from app.studio.search import (
    SearXNGSearchProvider,
    SearchCache,
    classify_engine_failure,
    classify_search_outcome,
)


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        studio_search_enabled=True,
        studio_search_base_url="http://searxng.test",
    )


def _payload(results=None, unresponsive=None):
    return {
        "query": "channel topic",
        "number_of_results": len(results or []),
        "results": results or [],
        "answers": [],
        "corrections": [],
        "infoboxes": [],
        "suggestions": [],
        "unresponsive_engines": unresponsive or [],
    }


def _result(engine="google", title="Channel topic update", url="https://news.test/story"):
    return {
        "url": url,
        "title": title,
        "content": "A bounded report about the channel topic.",
        "engine": engine,
    }


def _provider(payloads, *, status=200):
    calls: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        payload = payloads[min(len(calls) - 1, len(payloads) - 1)]
        if isinstance(payload, tuple):
            code, body = payload
            return httpx.Response(code, json=body)
        return httpx.Response(status, json=payload)

    transport = httpx.MockTransport(_handler)
    provider = SearXNGSearchProvider(
        _settings(), cache=SearchCache(ttl_seconds=0.0), transport=transport
    )
    provider.calls = calls  # type: ignore[attr-defined]
    return provider


@pytest.mark.asyncio
async def test_unrequested_engine_warning_keeps_batch_healthy():
    provider = _provider([_payload([_result("google")], unresponsive=[["bing", "CAPTCHA required"]])])
    response = await provider.search("channel topic", engines=("google",))
    assert response.outcome == "healthy"
    assert response.degraded is False
    assert [item["engine"] for item in response.engine_failures] == ["bing"]
    assert response.engine_failures[0]["relevant"] is False
    assert response.engine_failures[0]["code"] == "engine_captcha"
    assert response.engines_requested == ("google",)
    assert response.engines_executed == ("google",)
    assert len(response.results) == 1


@pytest.mark.asyncio
async def test_requested_engine_failure_marks_batch_partial():
    provider = _provider([_payload([_result("google")], unresponsive=[["google", "HTTP 429 Too Many Requests"]])])
    response = await provider.search("channel topic", engines=("google", "bing"))
    assert response.outcome == "partial"
    assert response.degraded is True
    relevant = [item for item in response.engine_failures if item["relevant"]]
    assert [item["engine"] for item in relevant] == ["google"]
    assert relevant[0]["code"] == "engine_rate_limited"
    assert len(response.results) == 1


@pytest.mark.asyncio
async def test_all_requested_engines_failing_is_unavailable():
    provider = _provider([_payload([], unresponsive=[["google", "connection timed out"], ["bing", "HTTP 503"]])])
    response = await provider.search("channel topic", engines=("google", "bing"))
    assert response.outcome == "unavailable"
    assert response.degraded is True
    assert response.results == ()


@pytest.mark.asyncio
async def test_clean_zero_result_query_is_empty_not_degraded():
    provider = _provider([_payload([])])
    response = await provider.search("channel topic nobody covers")
    assert response.outcome == "empty"
    assert response.degraded is False
    assert response.engine_failures == ()


@pytest.mark.asyncio
async def test_timeout_and_invalid_responses_are_unavailable():
    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connection timed out")

    provider = SearXNGSearchProvider(
        _settings(), cache=SearchCache(ttl_seconds=0.0),
        transport=httpx.MockTransport(_boom),
    )
    response = await provider.search("channel topic")
    assert response.outcome == "unavailable"
    assert response.degraded is True

    bad = _provider([(200, ["not", "a", "dict"])])
    response = await bad.search("channel topic")
    assert response.outcome == "unavailable"


@pytest.mark.asyncio
async def test_rejected_engine_selection_is_exposed_as_mismatch():
    provider = _provider([_payload([_result("google")])])
    response = await provider.search("channel topic", engines=("google", "no-such-engine"))
    assert response.outcome == "healthy"
    assert response.engine_selection_mismatch == ("no-such-engine",)
    assert response.engines_requested == ("google", "no-such-engine")
    assert "no-such-engine" not in response.engines_executed


@pytest.mark.asyncio
async def test_mixed_parallel_variants_aggregate_without_losing_success():
    from app.studio.search import SearchQuery, SearchResponse

    async def _variant(outcome, count):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        from app.studio.search import SearchResult

        return SearchResponse(
            query=SearchQuery(text="q"),
            results=tuple(
                SearchResult(
                    url=f"https://news.test/{i}", canonical_url=f"https://news.test/{i}",
                    title="t", snippet="s", source_name="n", domain="news.test",
                    published_at=None, provider="fixture", query="q", fetched_at=now,
                    result_index=i, source_id=f"s-{i}",
                )
                for i in range(count)
            ),
            outcome=outcome,
            engine_failures=(
                ({"engine": "bing", "code": "engine_rate_limited", "reason": "429", "relevant": True},)
                if outcome == "partial" else ()
            ),
        )

    healthy = await _variant("healthy", 2)
    partial = await _variant("partial", 1)
    summary = ResearchService.aggregate_search_outcomes([healthy, partial])
    assert summary["outcome"] == "partial"
    assert summary["failed_engines"] == ["bing"]
    assert [v["result_count"] for v in summary["variants"]] == [2, 1]


def test_outcome_classifier_matrix():
    assert classify_search_outcome(result_count=3, relevant_failures=0, transport_failed=False) == "healthy"
    assert classify_search_outcome(result_count=3, relevant_failures=1, transport_failed=False) == "partial"
    assert classify_search_outcome(result_count=0, relevant_failures=0, transport_failed=False) == "empty"
    assert classify_search_outcome(result_count=0, relevant_failures=2, transport_failed=False) == "unavailable"
    assert classify_search_outcome(result_count=0, relevant_failures=0, transport_failed=True) == "unavailable"
    assert classify_engine_failure("CAPTCHA challenge") == "engine_captcha"
    assert classify_engine_failure("HTTP 429") == "engine_rate_limited"
    assert classify_engine_failure("timed out") == "engine_timeout"
    assert classify_engine_failure("HTTP 503 Bad Gateway") == "engine_http_error"
    assert classify_engine_failure("weird opaque message") == "engine_unavailable"


@pytest.mark.asyncio
async def test_agent_query_text_and_result_order_stay_under_agent_control():
    payload = _payload([
        _result("google", title="Second", url="https://news.test/second"),
        _result("bing", title="First", url="https://news.test/first"),
    ])
    provider = _provider([payload])
    response = await provider.search("моя формулировка запроса", engines=("bing", "google"), language="ru")
    assert [item.title for item in response.results] == ["Second", "First"]
    assert provider.calls[0]["q"] == "моя формулировка запроса"
    assert provider.calls[0]["language"] == "ru"
    assert set(provider.calls[0]["engines"].split(",")) == {"bing", "google"}
