from datetime import datetime, timezone

import pytest
import pytest_asyncio
import httpx
from fastapi import FastAPI

from app.config import Settings
from app.db import Database
from app.web.routes import create_app


@pytest.fixture(autouse=True)
def isolate_tests_from_operator_env(monkeypatch):
    """Keep private deployment settings from changing default-state tests."""

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

    def __init__(self, db: Database) -> None:
        self.db = db

    async def poll_all(self, reason: str = "test") -> dict[str, object]:
        return {"reason": reason, "posts_seen": 0, "snapshots_written": 0}


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        api_id=1,
        api_hash="test-api-hash",
        session_string="test-session",
        channels="@sample_channel",
        data_dir=str(tmp_path),
        admin_username="test-admin",
        admin_password="test-password",
        session_secret="test-session-secret",
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    db = Database(settings.db_path)
    db.init_db()
    channel_id = db.upsert_channel(
        "@sample_channel", title="Sample channel", chat_id=123456
    )
    post_id = db.upsert_post(
        channel_id,
        message_id=42,
        posted_at=datetime(2024, 1, 2, 12, 30, tzinfo=timezone.utc),
        text="A representative collected post",
    )
    db.add_snapshot_if_changed(post_id, views=120, comments=4, reactions=8, shares=2)
    return create_app(FakeCollector(db), settings)


@pytest_asyncio.fixture
async def client(app: FastAPI):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as test_client:
        yield test_client
