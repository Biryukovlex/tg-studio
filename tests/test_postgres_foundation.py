import os
import uuid

import pytest

from app.db_session import normalize_database_url
from app.postgres_models import Base
from app.session_crypto import SessionCipher, build_cipher


def test_postgres_url_normalization_and_model_inventory():
    assert normalize_database_url("postgresql://u:p@localhost/db").startswith("postgresql+asyncpg://")
    assert normalize_database_url("postgres://u:p@localhost/db").startswith("postgresql+asyncpg://")
    assert "workspaces" in Base.metadata.tables
    assert "collection_jobs" in Base.metadata.tables
    assert "workspace_id" in Base.metadata.tables["posts"].c
    assert "formatting_entities" in Base.metadata.tables["posts"].c
    assert "workspace_id" in Base.metadata.tables["comments"].c


def test_session_cipher_round_trip_and_wrong_key_rejection():
    cipher = SessionCipher("a" * 48)
    token = cipher.encrypt("telethon-session-value")
    assert token != b"telethon-session-value"
    assert cipher.decrypt(token) == "telethon-session-value"
    with pytest.raises(ValueError, match="could not be decrypted"):
        SessionCipher("b" * 48).decrypt(token)
    assert build_cipher("") is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_repository_smoke():
    database_url = os.environ.get("M1_POSTGRES_URL") or os.environ.get("M0_POSTGRES_URL")
    if not database_url:
        pytest.skip("set M1_POSTGRES_URL to run the PostgreSQL repository proof")
    from datetime import datetime, timezone
    from app.postgres_db import PostgresDatabase

    db = PostgresDatabase(database_url)
    try:
        await db.init_db(admin_username="m1-test-admin")
        context = await db.workspace_context(username="m1-test-admin")
        assert context["workspace_slug"] == "community" and context["role"] == "owner"
        from app.session_crypto import SessionCipher
        cipher = SessionCipher("m1-session-secret" * 4)
        await db.persist_telegram_session(
            label="m1-test", api_id=1, api_hash="hash", session_string="encrypted-session", cipher=cipher
        )
        assert await db.load_telegram_session(label="m1-test", cipher=cipher) == "encrypted-session"
        channel_id = await db.upsert_channel("@m1-test", "M1 test", 1001)
        post_id = await db.upsert_post(
            channel_id,
            1,
            datetime.now(timezone.utc),
            "x" * 800,
            formatting_entities=[{"type": "bold", "offset": 0, "length": 4}],
        )
        assert await db.add_snapshot_if_changed(post_id, 10, 2, 3, 4)
        assert not await db.add_snapshot_if_changed(post_id, 10, 2, 3, 4)
        assert (await db.latest_stats(channel_id=channel_id))[0]["text"] == "x" * 800
        assert (await db.post_row(post_id))["formatting_entities"] == [
            {"type": "bold", "offset": 0, "length": 4}
        ]
        assert (await db.history_diagnostic(channel_id=channel_id))["posts_over_500"] == 1
        changed = await db.upsert_comment(
            post_id=post_id,
            telegram_message_id=2,
            discussion_chat_id=1002,
            discussion_username="fixture_discussion",
            sender_id=3,
            sender_name="Fixture author",
            sender_username="author",
            posted_at=datetime.now(timezone.utc),
            edited_at=None,
            text="A comment body",
            media_type="",
            reactions=1,
            reply_to_message_id=None,
            sync_token="m1-test",
        )
        assert changed and len(await db.comments_for_post(post_id)) == 1
        first = await db.claim_collection_job(channel_id)
        assert first is not None
        assert await db.claim_collection_job(channel_id) is None
        await db.finish_collection_job(first)
    finally:
        await db.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sqlite_importer_is_idempotent_and_reports_source_untouched(tmp_path):
    database_url = os.environ.get("M1_POSTGRES_URL") or os.environ.get("M0_POSTGRES_URL")
    if not database_url:
        pytest.skip("set M1_POSTGRES_URL to run the SQLite importer proof")
    import sqlite3
    from app.db import _SCHEMA
    from app.migration.sqlite_to_postgres import import_sqlite

    source = tmp_path / "archive.db"
    connection = sqlite3.connect(source)
    connection.executescript(_SCHEMA)
    connection.execute("INSERT INTO channels(id, identifier, title, chat_id) VALUES (10001, ?, ?, ?)", ("@importer", "Importer", 1))
    connection.execute(
        "INSERT INTO posts(id, channel_id, message_id, posted_at, text) VALUES (10001, 10001, 7, ?, ?)",
        ("2024-01-01 00:00:00", "ю" * 700),
    )
    connection.execute(
        "INSERT INTO snapshots(id, post_id, taken_at, views, comments, reactions, shares) VALUES (10001, 10001, ?, 1, 2, 3, 4)",
        ("2024-01-01 01:00:00",),
    )
    connection.commit()
    connection.close()
    before = source.stat()
    first = await import_sqlite(source, database_url, workspace_slug="importer-test")
    second = await import_sqlite(source, database_url, workspace_slug="importer-test")
    after = source.stat()
    assert first["all_match"] and second["all_match"]
    assert first["source_untouched"] and second["source_untouched"]
    assert first["diagnostics"]["posts"]["long_text_posts"] == 1
    assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_composite_foreign_key_rejects_cross_workspace_post():
    database_url = os.environ.get("M1_POSTGRES_URL") or os.environ.get("M0_POSTGRES_URL")
    if not database_url:
        pytest.skip("set M1_POSTGRES_URL to run tenant-isolation proof")
    from datetime import datetime, timezone
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    from app.postgres_db import PostgresDatabase

    db = PostgresDatabase(database_url)
    try:
        await db.init_db(admin_username="tenant-test-admin")
        other = uuid.uuid4()
        async with db.sessions.session() as session:
            await session.execute(text("INSERT INTO workspaces(id, slug, name) VALUES (:id, :slug, 'Other')"), {"id": other, "slug": f"other-{other.hex[:8]}"})
            row = await session.execute(
                text("""INSERT INTO channels(workspace_id, identifier, title, chat_id, created_at)
                         VALUES (:workspace_id, :identifier, 'Other', 2, now()) RETURNING id"""),
                {"workspace_id": other, "identifier": f"@other-{other.hex[:8]}"},
            )
            other_channel_id = row.scalar_one()
            await session.commit()
        with pytest.raises(IntegrityError):
            await db.upsert_post(other_channel_id, 1, datetime.now(timezone.utc), "must fail")
    finally:
        await db.close()
