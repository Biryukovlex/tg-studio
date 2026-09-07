from __future__ import annotations

import json
import re

import pytest


async def _login(client, settings):
    response = await client.post(
        "/login",
        data={"username": settings.admin_username, "password": settings.admin_password},
        follow_redirects=False,
    )
    assert response.status_code == 303


@pytest.mark.asyncio
async def test_research_health_and_bootstrap_never_block_on_optional_search(client, settings):
    settings.studio_test_mode = True
    settings.studio_search_enabled = True
    settings.studio_search_base_url = ""
    await _login(client, settings)
    health = await client.get("/studio/api/research/health")
    assert health.status_code == 200
    body = health.json()
    assert body["degraded"] is True
    assert body["configured"] is False
    assert "api_key" not in json.dumps(body)

    bootstrap = await client.get("/studio/api/bootstrap")
    assert bootstrap.status_code == 200
    assert bootstrap.json()["setup"]["ready"] is True
    assert bootstrap.json()["research"]["degraded"] is True


@pytest.mark.asyncio
async def test_research_health_preserves_authentication(client, settings):
    response = await client.get("/studio/api/research/health", follow_redirects=False)
    assert response.status_code == 401
    body = response.json()
    err = body.get("error") or body.get("detail", {}).get("error", {})
    assert err.get("code") == "unauthenticated"
