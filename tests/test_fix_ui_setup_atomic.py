"""Regression tests for UI-first setup and atomic settings submissions."""

from __future__ import annotations

import re
import os
import uuid
from unittest.mock import AsyncMock, call

import httpx
import pytest

from app.config import Settings
from app.db import Database
from app.main import resolve_telegram_connection, telegram_connection_problems
from app.session_crypto import build_cipher
from app.web.routes import create_app
from app.workspace_settings import WorkspaceSettings
from tests.test_fix_t23 import FakeDB, _make_app_with_fake

TEST_POSTGRES_URL = os.getenv("TEST_POSTGRES_URL", "")


async def _login(client: httpx.AsyncClient) -> str:
    response = await client.post("/login", data={"username": "admin", "password": "pw"})
    assert response.status_code == 303
    page = await client.get("/settings")
    return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)


def test_saved_telegram_connection_has_priority_over_environment() -> None:
    settings = Settings(_env_file=None, api_id=11, api_hash="env-hash", session_string="env-session")
    resolved = resolve_telegram_connection(
        settings,
        {"api_id": 22, "api_hash": "saved-hash", "session_string": "saved-session"},
    )
    assert resolved == {
        "api_id": 22,
        "api_hash": "saved-hash",
        "session_string": "saved-session",
    }
    assert telegram_connection_problems(resolved) == []


def test_postgres_startup_can_defer_telegram_for_ui_setup() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://example/test",
        admin_password="pw",
        api_id=0,
        api_hash="",
        session_string="",
    )
    assert settings.validate_required(defer_telegram=True) == []
    unresolved = resolve_telegram_connection(settings)
    assert telegram_connection_problems(unresolved) == [
        "Telegram API ID and API hash are missing.",
        "Telegram session string is missing.",
    ]


@pytest.mark.asyncio
async def test_load_complete_telegram_connection() -> None:
    cipher = build_cipher("x" * 32)
    encrypted = cipher.encrypt("saved-session")

    class Result:
        def mappings(self):
            return self

        def first(self):
            return {"api_id": 42, "api_hash": "saved-hash", "encrypted_session": encrypted}

    class DB:
        _execute = AsyncMock(return_value=Result())

    from app.postgres_db import PostgresDatabase

    connection = await PostgresDatabase.load_telegram_connection(DB(), label="default", cipher=cipher)
    assert connection == {"api_id": 42, "api_hash": "saved-hash", "session_string": "saved-session"}


@pytest.mark.asyncio
async def test_invalid_collection_form_writes_nothing(tmp_path) -> None:
    app, store, _db = _make_app_with_fake(tmp_path, role="owner", available=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client)
        response = await client.post(
            "/settings/collection",
            data={"poll_minutes": "30", "track_days": "not-a-number", "backfill_limit": "500", "csrf_token": token},
        )
    assert response.status_code == 422
    assert store._calls == []


@pytest.mark.asyncio
async def test_invalid_studio_form_does_not_clear_key_or_prompt(tmp_path) -> None:
    app, store, _db = _make_app_with_fake(tmp_path, role="owner", available=True)
    app.state.studio_repository.set_system_prompt = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client)
        response = await client.post(
            "/settings/studio",
            data={
                "clear_openrouter_api_key": "1",
                "model": "invalid model",
                "system_prompt": "Keep it concise.",
                "csrf_token": token,
            },
        )
    assert response.status_code == 422
    assert store._calls == []
    app.state.studio_repository.set_system_prompt.assert_not_awaited()


@pytest.mark.asyncio
async def test_studio_prompt_is_restored_when_settings_write_fails(tmp_path) -> None:
    from app.workspace_settings import EncryptionKeyRequired

    app, store, _db = _make_app_with_fake(tmp_path, role="owner", available=True)
    app.state.studio_repository.get_system_prompt = AsyncMock(return_value="Previous prompt")
    app.state.studio_repository.set_system_prompt = AsyncMock()
    store.set = AsyncMock(side_effect=EncryptionKeyRequired("missing encryption key"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client)
        response = await client.post(
            "/settings/studio",
            data={
                "openrouter_api_key": "new-key",
                "model": "openai/gpt-4o-mini",
                "system_prompt": "New prompt",
                "csrf_token": token,
            },
        )
    assert response.status_code == 409
    assert app.state.studio_repository.set_system_prompt.await_args_list == [
        call("New prompt"),
        call("Previous prompt"),
    ]


@pytest.mark.asyncio
async def test_invalid_research_form_writes_nothing(tmp_path) -> None:
    app, store, _db = _make_app_with_fake(tmp_path, role="owner", available=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client)
        response = await client.post(
            "/settings/research",
            data={"research_enabled": "1", "blocked_domains": "https://not-a-host/path", "csrf_token": token},
        )
    assert response.status_code == 422
    assert store._calls == []


@pytest.mark.asyncio
async def test_any_telegram_connection_change_requires_restart(tmp_path) -> None:
    db = FakeDB()

    async def secrets(_label, *, cipher):
        return {"api_id": 123, "api_hash": "stored-hash", "session_string": "stored-session"}

    db.telegram_connection_secrets = secrets
    app, store, _db = _make_app_with_fake(tmp_path, role="owner", available=True, db=db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client)
        response = await client.post(
            "/settings/telegram-connection",
            data={"api_id": "456", "api_hash": "", "session_string": "", "csrf_token": token},
        )
    assert response.status_code == 303
    assert db._persist_called == ("default", 456, "stored-hash", "stored-session")
    assert store.telegram_restart_required is True


@pytest.mark.asyncio
async def test_refresh_redirects_to_settings_while_telegram_is_unconfigured(tmp_path) -> None:
    db = Database(tmp_path / "setup-mode.sqlite3")
    db.init_db()

    class SetupCollector:
        client = None

        def __init__(self):
            self.db = db
            self.poll_all = AsyncMock()

    collector = SetupCollector()
    settings = Settings(
        _env_file=None,
        admin_username="admin",
        admin_password="pw",
        session_secret="setup-mode-secret",
        data_dir=str(tmp_path),
    )
    app = create_app(collector, settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/login", data={"username": "admin", "password": "pw"})
        assert response.status_code == 303
        response = await client.post("/refresh")
    assert response.status_code == 303
    assert response.headers["location"] == "/settings#telegram"
    collector.poll_all.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_many_commits_once_and_updates_memory_after_commit() -> None:
    settings = Settings(_env_file=None, telegram_session_encryption_key="x" * 32)

    class Session:
        def __init__(self):
            self.execute = AsyncMock()
            self.commit = AsyncMock()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    session = Session()

    class Sessions:
        def session(self):
            return session

    class DB:
        is_postgres = True
        workspace_id = "workspace"
        sessions = Sessions()

    store = WorkspaceSettings(DB(), settings, build_cipher("x" * 32))
    await store.set_many(
        {
            "collection.poll_minutes": 30,
            "collection.track_days": 90,
            "collection.backfill_limit": 1000,
        }
    )
    assert session.execute.await_count == 3
    session.commit.assert_awaited_once()
    assert store.effective.poll_minutes == 30.0
    assert store.effective.track_days == 90
    assert store.effective.backfill_limit == 1000


@pytest.mark.skipif(not TEST_POSTGRES_URL, reason="set TEST_POSTGRES_URL to run the PostgreSQL UI-setup proof")
@pytest.mark.asyncio
async def test_postgres_connection_loading_and_invalid_batch_are_atomic() -> None:
    from sqlalchemy import text

    from app.postgres_db import PostgresDatabase

    slug = f"ui-setup-{uuid.uuid4().hex[:12]}"
    db = PostgresDatabase(TEST_POSTGRES_URL, workspace_slug=slug)
    await db.init_db(admin_username=f"admin-{slug}")
    cipher = build_cipher("ui-setup-proof-key")
    store = WorkspaceSettings(db, Settings(_env_file=None), cipher)
    await store.load()
    try:
        with pytest.raises(ValueError):
            await store.set_many(
                {
                    "collection.poll_minutes": 30,
                    "collection.track_days": "invalid",
                    "collection.backfill_limit": 1000,
                }
            )
        async with db.sessions.session() as session:
            count = (
                await session.execute(
                    text("SELECT count(*) FROM workspace_settings WHERE workspace_id=:workspace_id"),
                    {"workspace_id": db.workspace_id},
                )
            ).scalar_one()
        assert count == 0

        await store.set_many(
            {
                "collection.poll_minutes": 30,
                "collection.track_days": 90,
                "collection.backfill_limit": 1000,
            }
        )
        await db.persist_telegram_session(
            label="default",
            api_id=42,
            api_hash="saved-hash",
            session_string="saved-session",
            cipher=cipher,
        )
        assert await db.load_telegram_connection(label="default", cipher=cipher) == {
            "api_id": 42,
            "api_hash": "saved-hash",
            "session_string": "saved-session",
        }
    finally:
        async with db.sessions.session() as session:
            await session.execute(text("DELETE FROM workspaces WHERE id=:workspace_id"), {"workspace_id": db.workspace_id})
            await session.commit()
        await db.close()
