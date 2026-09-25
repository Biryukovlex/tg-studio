import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
import httpx
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings
from app.postgres_db import PostgresDatabase
from app.web.routes import create_app


def _pg_url() -> str:
    import os

    url = os.environ.get("TEST_POSTGRES_URL", "")
    if not url:
        pytest.skip("TEST_POSTGRES_URL is required")
    return url


def pytest_collection_modifyitems(items):
    """Mirror the postgres marker onto every integration test.

    PostgreSQL-gated tests are marked ``integration`` at their definition
    sites; CI runs them separately with ``pytest -m postgres`` against a
    disposable service. Keeping the mapping here (instead of editing every
    test) guarantees a new integration test is automatically included.
    """

    for item in items:
        if "integration" in item.keywords:
            item.add_marker(pytest.mark.postgres)


@pytest.fixture(autouse=True)
def isolate_tests_from_operator_env(monkeypatch):
    """Keep private deployment settings from changing default-state tests."""

    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for key in (
        "API_ID",
        "API_HASH",
        "SESSION_STRING",
        "OPENROUTER_API_KEY",
        "OPENROUTER_MODEL",
        "TELEGRAM_SESSION_ENCRYPTION_KEY",
        "TELEGRAM_SESSION_ENCRYPTION_KEY_PREVIOUS",
        "STUDIO_ENABLED",
        "STUDIO_TEST_MODE",
        "STUDIO_SEARCH_ENABLED",
        "STUDIO_SEARCH_BASE_URL",
        "STUDIO_SEARCH_BLOCKED_DOMAINS",
        "ADMIN_USERNAME",
        "ADMIN_PASSWORD",
        "SESSION_SECRET",
        "DATABASE_URL",
        "CHANNELS",
        "PROCESS_ROLE",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in {
        "DATABASE_URL": "",
        "OPENROUTER_API_KEY": "",
        "STUDIO_ENABLED": "false",
        "STUDIO_TEST_MODE": "false",
        "STUDIO_SEARCH_ENABLED": "false",
        "STUDIO_SEARCH_BASE_URL": "",
    }.items():
        monkeypatch.setenv(key, value)


class FakeCollector:
    """Small application boundary used by web tests; it never connects to Telegram."""

    def __init__(self, db: PostgresDatabase) -> None:
        self.db = db

    async def poll_all(self, reason: str = "test") -> dict[str, object]:
        return {"reason": reason, "posts_seen": 0, "snapshots_written": 0}


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        api_id=1,
        api_hash="test-api-hash",
        session_string="test-session",
        channels="@sample_channel",
        data_dir=str(tmp_path),
        admin_username="test-admin",
        admin_password="test-password",
        session_secret="test-session-secret",
        # PostgreSQL is the only runtime; Studio persistence needs the key.
        telegram_session_encryption_key="Wl0-cvos82PSFL7U0PbD7SrvsEJRKq6I1Y_APkFy3iM=",
    )


@pytest_asyncio.fixture
async def app(settings: Settings) -> FastAPI:
    """Web app on a per-test PostgreSQL workspace; the workspace is removed
    afterwards (cascades to channels, posts, snapshots, and Studio rows)."""
    url = _pg_url()
    # Assign in place (not model_copy): tests mutate this same object after
    # the app is built and the app must observe those changes.
    settings.database_url = url
    slug = f"t-{uuid.uuid4().hex[:12]}"
    db = PostgresDatabase(url, workspace_slug=slug)
    await db.init_db(admin_username=settings.admin_username)
    channel_id = await db.upsert_channel("@sample_channel", "Sample channel", 123456)
    post_id = await db.upsert_post(
        channel_id,
        message_id=42,
        posted_at=datetime(2024, 1, 2, 12, 30, tzinfo=timezone.utc),
        text="A representative collected post",
    )
    await db.add_snapshot_if_changed(post_id, views=120, comments=4, reactions=8, shares=2)
    app = create_app(FakeCollector(db), settings)
    app.state.test_channel_id = channel_id
    try:
        yield app
    finally:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                workspace_id = await conn.execute(
                    text("SELECT id FROM workspaces WHERE slug=:slug"), {"slug": slug}
                )
                row = workspace_id.first()
                if row is not None:
                    await conn.execute(text("DELETE FROM workspaces WHERE id=:id"), {"id": row[0]})
                    await conn.commit()
        finally:
            await engine.dispose()
        await db.close()


@pytest_asyncio.fixture
async def client(app: FastAPI):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as test_client:
        yield test_client


@pytest_asyncio.fixture
async def channel_id(app: FastAPI) -> int:
    """Database id of the seeded @sample_channel (PG ids are not 1)."""
    return int(app.state.test_channel_id)
