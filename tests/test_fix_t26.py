"""T26 acceptance tests for live poll-interval rescheduling."""

from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.collector import Collector
from tests.test_fix_t23 import _make_app_with_fake


@pytest.mark.asyncio
async def test_poll_all_detects_saved_interval_against_applied_scheduler_value():
    db = SimpleNamespace(get_channels=AsyncMock(return_value=[]))
    settings = SimpleNamespace(poll_minutes=5.0)

    class Workspace:
        async def load(self):
            return None

    collector = Collector(None, db, settings)
    collector.workspace_settings = Workspace()
    collector.applied_poll_minutes = 15.0
    calls: list[float] = []

    def on_change(minutes: float) -> None:
        calls.append(minutes)
        collector.applied_poll_minutes = minutes

    collector.on_poll_interval_change = on_change

    await collector.poll_all(reason="test")
    await collector.poll_all(reason="test")

    assert calls == [5.0]


@pytest.mark.asyncio
async def test_collection_save_notifies_live_scheduler_callback(tmp_path):
    app, _store, _db = _make_app_with_fake(tmp_path, role="owner", available=True)
    callback = AsyncMock()
    app.state.on_poll_interval_change = callback

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post("/login", data={"username": "admin", "password": "pw"})
        assert login.status_code == 303
        page = await client.get("/settings")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        response = await client.post(
            "/settings/collection",
            data={
                "poll_minutes": "5",
                "track_days": "30",
                "backfill_limit": "200",
                "csrf_token": token,
            },
        )

    assert response.status_code == 303
    callback.assert_awaited_once_with(5.0)
