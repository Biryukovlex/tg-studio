"""Integration review fixes for T21–T23 (settings store, page and readiness)."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import Settings
from app.studio.setup import build_setup_state
from app.workspace_settings import WorkspaceSettings, format_timestamp

from tests.test_fix_t23 import FakeDB, FakeWorkspaceSettings, _make_app_with_fake


class _StoreDB:
    """Minimal database double: PostgreSQL flags without a SQL executor."""

    is_postgres = True

    def __init__(self) -> None:
        self.workspace_id = uuid.uuid4()


def test_format_timestamp_handles_datetimes_and_strings():
    stamp = datetime(2026, 9, 6, 21, 14, 55, tzinfo=timezone.utc)
    assert format_timestamp(stamp) == "2026-09-06 21:14"
    assert format_timestamp("2026-09-05 14:02:00+00:00") == "2026-09-05 14:02"
    assert format_timestamp(None) is None


@pytest.mark.asyncio
async def test_set_stamps_updated_at_and_as_dict_renders_it_as_text():
    store = WorkspaceSettings(_StoreDB(), Settings(_env_file=None), None)
    await store.load()
    assert store.available
    await store.set("collection.poll_minutes", "30")
    entry = store.as_dict()["collection.poll_minutes"]
    assert entry["source"] == "db" and entry["value"] == 30.0
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", entry["updated_at"]), entry
    # A row loaded from PostgreSQL carries a datetime; the template gets text.
    store._rows["collection.track_days"] = {"value": 7, "is_secret": False, "updated_at": datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc), "raw": "7"}
    assert store.as_dict()["collection.track_days"]["updated_at"] == "2026-09-07 08:00"


def test_setup_state_accepts_channels_added_on_the_settings_page():
    settings = Settings(_env_file=None, openrouter_api_key="k", telegram_session_encryption_key="x" * 32, channels="")

    class _DB:
        is_postgres = True
        workspace_slug = "community"
        active_channel_count = 2

    assert build_setup_state(settings, _DB())["ready"] is True
    _DB.active_channel_count = 0
    blockers = build_setup_state(settings, _DB())["blockers"]
    assert [b["code"] for b in blockers] == ["channel_missing"]
    assert "Settings" in blockers[0]["message"]


async def _login(client: httpx.AsyncClient) -> str:
    await client.post("/login", data={"username": "admin", "password": "pw"})
    page = await client.get("/settings")
    assert page.status_code == 200
    return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)


@pytest.mark.asyncio
async def test_settings_page_prefills_the_system_prompt_and_keeps_it_on_save(tmp_path):
    app, ws, db = _make_app_with_fake(tmp_path, role="owner", available=True)
    app.state.studio_repository.get_system_prompt = AsyncMock(return_value="Keep posts short.")
    app.state.studio_repository.set_system_prompt = AsyncMock(return_value="Keep posts short.")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client)
        page = await client.get("/settings")
        assert ">Keep posts short.</textarea>" in page.text
        # The browser submits the prefilled prompt back; nothing is erased.
        resp = await client.post(
            "/settings/studio",
            data={"openrouter_api_key": "", "model": "openai/gpt-4o-mini", "system_prompt": "Keep posts short.", "csrf_token": token},
        )
        assert resp.status_code == 303
        app.state.studio_repository.set_system_prompt.assert_awaited_once_with("Keep posts short.")


@pytest.mark.asyncio
async def test_settings_page_uses_hashed_assets_and_shows_the_configured_search_url(tmp_path):
    app, ws, db = _make_app_with_fake(tmp_path, role="owner", available=True)
    app.state.settings.studio_search_base_url = "http://searxng:8080"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await _login(client)
        page = await client.get("/settings")
        version = re.search(r'/static/style\.css\?v=([^"]+)"', page.text)
        assert version and len(version.group(1)) >= 8, "settings page must carry the content-hash asset version"
        assert "<code>http://searxng:8080</code>" in page.text


@pytest.mark.asyncio
async def test_settings_page_lists_only_active_channels(tmp_path):
    db = FakeDB()
    db._channels.append({"id": 3, "identifier": "@retired", "title": "Retired", "chat_id": -100555, "active": False})
    app, ws, _ = _make_app_with_fake(tmp_path, role="owner", available=True, db=db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await _login(client)
        page = await client.get("/settings")
        assert "@city_digest" in page.text and "@retired" not in page.text


@pytest.mark.asyncio
async def test_connection_edit_merges_with_the_stored_connection(tmp_path):
    db = FakeDB()

    async def secrets(label, *, cipher):
        return {"api_id": 123456, "api_hash": "stored-hash", "session_string": "stored-session"}

    db.telegram_connection_secrets = secrets
    app, ws, _ = _make_app_with_fake(tmp_path, role="owner", available=True, db=db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client)
        # Only a new session: id and hash come from the stored row, never a placeholder.
        resp = await client.post(
            "/settings/telegram-connection",
            data={"api_id": "", "api_hash": "", "session_string": "fresh-session", "csrf_token": token},
        )
        assert resp.status_code == 303
        assert db._persist_called == ("default", 123456, "stored-hash", "fresh-session")
        assert ws.telegram_restart_required is True
        assert "stored-hash" not in (await client.get("/settings")).text
        # Nothing entered at all is a validation error, not a silent success.
        resp = await client.post("/settings/telegram-connection", data={"csrf_token": token})
        assert resp.status_code == 422
        assert "Enter an API ID, an API hash, or a session string" in resp.text


@pytest.mark.asyncio
async def test_connection_requires_all_fields_when_nothing_is_stored(tmp_path):
    db = FakeDB()

    async def secrets(label, *, cipher):
        return None

    db.telegram_connection_secrets = secrets
    app, ws, _ = _make_app_with_fake(tmp_path, role="owner", available=True, db=db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client)
        resp = await client.post(
            "/settings/telegram-connection",
            data={"api_id": "42", "api_hash": "", "session_string": "", "csrf_token": token},
        )
        assert resp.status_code == 422
        assert "Required for a new connection." in resp.text
        assert not hasattr(db, "_persist_called")


@pytest.mark.asyncio
async def test_reset_is_scoped_to_its_section_and_reports_the_reset(tmp_path):
    app, ws, db = _make_app_with_fake(tmp_path, role="owner", available=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client)
        resp = await client.post("/settings/studio/reset", data={"key": "collection.poll_minutes", "csrf_token": token})
        assert resp.status_code == 422
        assert ("reset", "collection.poll_minutes") not in ws._calls
        resp = await client.post("/settings/collection/reset", data={"key": "collection.poll_minutes", "csrf_token": token})
        assert resp.status_code == 303
        page = await client.get("/settings")
        assert "Setting reset to the .env value." in page.text
