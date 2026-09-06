from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.studio.search_health import check_search_health, configured_search_state
from app.studio.setup import build_setup_state


def test_search_setup_is_optional_and_does_not_add_a_blocker():
    settings = Settings(studio_enabled=True, studio_search_enabled=True, studio_search_base_url="")
    state = configured_search_state(settings).as_dict()
    assert state["enabled"] is True
    assert state["configured"] is False
    assert state["degraded"] is True
    assert "private" in state["message"].lower()

    class Db:
        is_postgres = True
        workspace_slug = "community"

    setup = build_setup_state(settings, Db())
    assert setup["research"]["degraded"] is True
    assert all(item["code"] != "search_unavailable" for item in setup["blockers"])


@pytest.mark.asyncio
async def test_health_probe_is_bounded_and_returns_safe_state():
    settings = Settings(
        studio_search_enabled=True,
        studio_search_base_url="http://searxng:8080/private-path",
        studio_search_timeout_seconds=2,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/healthz"
        return httpx.Response(200, json={"private": "payload omitted"})

    result = await check_search_health(settings, transport=httpx.MockTransport(handler))
    assert result.available is True
    assert result.degraded is False
    assert result.endpoint == "http://searxng:8080"
    assert "payload" not in result.message


@pytest.mark.asyncio
async def test_health_probe_failure_is_degraded_without_private_error_details():
    settings = Settings(studio_search_enabled=True, studio_search_base_url="http://searxng:8080")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="secret upstream diagnostics")

    result = await check_search_health(settings, transport=httpx.MockTransport(handler))
    assert result.available is False
    assert result.degraded is True
    assert "secret" not in result.message
