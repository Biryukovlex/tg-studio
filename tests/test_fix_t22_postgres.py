"""T22 Postgres acceptance tests – require TEST_POSTGRES_URL."""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio

from app.config import Settings

pytestmark = pytest.mark.skipif(not os.getenv("TEST_POSTGRES_URL"), reason="set TEST_POSTGRES_URL to run the PostgreSQL proof")

TEST_URL = os.getenv("TEST_POSTGRES_URL")


@pytest.mark.asyncio
async def test_migration_0010_upgrades_and_downgrades():
    # Test migration upgrade/downgrade on disposable DB
    from alembic.config import Config
    from alembic import command
    from sqlalchemy import text
    from app.db_session import DatabaseSessionManager

    url = TEST_URL
    assert url
    # Use DatabaseSessionManager to check current version
    from app.postgres_db import PostgresDatabase

    # Create a fresh DB manager for migration test - use the same URL but with a separate check
    # We will test that 0010 can be applied and reverted via alembic
    # For simplicity, check that the table exists after upgrade and not after downgrade
    # First, ensure current head is applied
    mgr = DatabaseSessionManager(url)
    async with mgr.session() as session:
        result = await session.execute(text("SELECT version_num FROM alembic_version"))
        version = result.scalar_one_or_none()
        assert version == "0010_workspace_settings" or version is not None
    await mgr.dispose()
    # Downgrade to 0009 and upgrade back
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", url.replace("postgresql+asyncpg://", "postgresql://"))
    # Use command.downgrade and upgrade (sync, need to run in thread)
    import asyncio

    def downgrade():
        command.downgrade(alembic_cfg, "0009_profile_text")

    def upgrade():
        command.upgrade(alembic_cfg, "head")

    await asyncio.to_thread(downgrade)
    # Check downgrade
    mgr2 = DatabaseSessionManager(url)
    async with mgr2.session() as session:
        result = await session.execute(text("SELECT to_regclass('public.workspace_settings')"))
        assert result.scalar_one_or_none() is None
    await mgr2.dispose()
    await asyncio.to_thread(upgrade)
    mgr3 = DatabaseSessionManager(url)
    async with mgr3.session() as session:
        result = await session.execute(text("SELECT to_regclass('public.workspace_settings')"))
        assert result.scalar_one_or_none() == "workspace_settings"
        # Check head version again
        result2 = await session.execute(text("SELECT version_num FROM alembic_version"))
        assert result2.scalar_one_or_none() == "0010_workspace_settings"
    await mgr3.dispose()


@pytest.mark.asyncio
async def test_set_reset_roundtrip_and_secret_isolation():
    from app.postgres_db import PostgresDatabase
    from app.workspace_settings import WorkspaceSettings
    from app.session_crypto import build_cipher

    url = TEST_URL
    assert url
    # Create two workspaces with different slugs to test isolation
    settings1 = Settings(_env_file=None, database_url=url, telegram_session_encryption_key="x" * 32, local_workspace_slug="test-ws1")
    # Use direct PostgresDatabase with custom slug - need to handle via limits? For test, use default and manually set slug
    # Instead, use the same DB but different workspace_id via direct manipulation
    db1 = PostgresDatabase(url, workspace_slug="test-ws1")
    await db1.init_db(admin_username="admin1")
    from app.session_crypto import build_cipher

    cipher1 = build_cipher("x" * 32)
    ws1 = WorkspaceSettings(db1, Settings(_env_file=None, poll_minutes=15, track_days=30, backfill_limit=200, openrouter_api_key="", openrouter_model="openai/gpt-4o-mini", studio_search_enabled=False, studio_search_blocked_domains=""), cipher1)
    await ws1.load()
    # Round-trip for every registry key
    await ws1.set("collection.poll_minutes", 30.0)
    await ws1.set("collection.track_days", 100)
    await ws1.set("collection.backfill_limit", 5000)
    await ws1.set("studio.model", "custom/model-test")
    await ws1.set("research.enabled", True)
    await ws1.set("research.blocked_domains", "x.com, y.org")
    # Secret
    await ws1.set("studio.openrouter_api_key", "secret-123")
    # Verify as_dict
    d = ws1.as_dict()
    assert d["collection.poll_minutes"]["value"] == 30.0
    assert d["collection.poll_minutes"]["source"] == "db"
    assert d["research.blocked_domains"]["value"] == "x.com,y.org"
    assert d["studio.openrouter_api_key"]["set"] is True
    # Check DB stores encrypted token not plaintext
    from sqlalchemy import text as sql_text

    async with db1.sessions.session() as session:
        result = await session.execute(sql_text("SELECT value, is_secret FROM workspace_settings WHERE workspace_id=:wid AND key=:key"), {"wid": db1.workspace_id, "key": "studio.openrouter_api_key"})
        row = result.mappings().first()
        assert row is not None
        assert row["is_secret"] is True
        assert row["value"] != "secret-123"
        # Decrypt and verify
        token = row["value"].encode("utf-8")
        assert cipher1.decrypt(token) == "secret-123"
    # Reset and verify
    await ws1.reset("collection.poll_minutes")
    d2 = ws1.as_dict()
    assert d2["collection.poll_minutes"]["source"] in ("env", "default")
    # Second workspace isolation
    db2 = PostgresDatabase(url, workspace_slug="test-ws2")
    await db2.init_db(admin_username="admin2")
    ws2 = WorkspaceSettings(db2, Settings(_env_file=None), cipher1)
    await ws2.load()
    d2_ws2 = ws2.as_dict()
    # ws2 should not see ws1's rows
    assert d2_ws2["collection.poll_minutes"]["source"] != "db" or d2_ws2["collection.poll_minutes"].get("value") != 30.0
    # Cleanup
    await db1.close()
    await db2.close()


@pytest.mark.asyncio
async def test_add_and_deactivate_channel():
    from app.postgres_db import PostgresDatabase
    from app.studio.repository import StudioRepository

    url = TEST_URL
    assert url
    settings = Settings(_env_file=None, database_url=url, telegram_session_encryption_key="x" * 32)
    db = PostgresDatabase(url, workspace_slug="test-channel-ws")
    await db.init_db(admin_username="admin-channel")
    # Ensure clean: deactivate any existing test channels
    # Add channel
    chan_id = await db.add_channel("@new_test_channel")
    assert chan_id
    channels = await db.get_channels()
    assert any(c["identifier"] == "@new_test_channel" for c in channels)
    # Studio list_channels should also see it
    repo = StudioRepository(db)
    studio_channels = await repo.list_channels()
    assert any(c["identifier"] == "@new_test_channel" for c in studio_channels)
    # Create a post for that channel to test retention
    # Use a dummy post
    from datetime import datetime, timezone

    post_id = await db.upsert_post(chan_id, 123, datetime.now(timezone.utc), "test post")
    assert post_id
    # Deactivate
    ok = await db.deactivate_channel(chan_id)
    assert ok is True
    channels2 = await db.get_channels()
    assert not any(c["identifier"] == "@new_test_channel" for c in channels2)
    studio_channels2 = await repo.list_channels()
    assert not any(c["identifier"] == "@new_test_channel" for c in studio_channels2)
    # Posts remain
    row = await db.post_row(post_id)
    assert row is not None
    # Reactivate
    chan_id2 = await db.add_channel("@new_test_channel")
    assert chan_id2 == chan_id
    channels3 = await db.get_channels()
    assert any(c["identifier"] == "@new_test_channel" for c in channels3)
    await db.close()


@pytest.mark.asyncio
async def test_validate_required_with_channels():
    from app.postgres_db import PostgresDatabase

    url = TEST_URL
    assert url
    # Test with CHANNELS empty but DB has channel
    settings = Settings(_env_file=None, database_url=url, channels="", api_id=1, api_hash="h", session_string="s", admin_password="pw", telegram_session_encryption_key="x" * 32)
    db = PostgresDatabase(url, workspace_slug="test-validate-ws")
    await db.init_db(admin_username="admin-validate")
    # Ensure no channels
    # Clean up existing channels for this workspace
    from sqlalchemy import text as sql_text

    async with db.sessions.session() as session:
        await session.execute(sql_text("DELETE FROM channels WHERE workspace_id=:wid"), {"wid": db.workspace_id})
        await session.commit()
    # Now no channels, validate should fail
    problems = settings.validate_required(db_channels=[])
    assert any("CHANNELS" in p for p in problems)
    # Add channel
    await db.add_channel("@test-validate")
    channels = await db.get_channels()
    assert channels
    problems2 = settings.validate_required(db_channels=channels)
    assert not any("CHANNELS" in p for p in problems2)
    await db.close()
