"""T09 acceptance tests: collection data integrity."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.collector import Collector
from app.config import Settings


class FakeMsg:
    def __init__(self, mid, date):
        self.id = mid
        self.date = date
        self.message = f"post {mid}"
        self.action = None
        self.entities = None
        self.views = 5
        self.replies = SimpleNamespace(replies=0)
        self.reactions = SimpleNamespace(results=[])
        self.forwards = 0


class FakeClient:
    def __init__(self, messages):
        self._messages = list(messages)

    async def get_entity(self, identifier):
        return SimpleNamespace(title="T", id=123)

    def iter_messages(self, entity, limit=None):
        messages = self._messages

        async def gen():
            for message in messages:
                yield message

        return gen()


class FakeDB:
    is_postgres = False

    def __init__(self):
        self.posts: dict[int, dict] = {}
        self._next_post_id = 1

    async def upsert_channel(self, *args, **kwargs):
        return 1

    async def upsert_post(self, channel_db_id, message_id, posted_at, text, formatting_entities=None):
        for post_id, row in self.posts.items():
            if row["message_id"] == message_id:
                row.update({"is_deleted": False, "deleted_at": None})
                return post_id
        post_id = self._next_post_id
        self._next_post_id += 1
        self.posts[post_id] = {"message_id": message_id, "is_deleted": False, "deleted_at": None}
        return post_id

    async def add_snapshot_if_changed(self, *args, **kwargs):
        return False

    async def has_comments(self, post_id):
        return False

    async def mark_unseen_posts_deleted(self, channel_id, seen_message_ids, since=None, min_message_id=None):
        seen = set(seen_message_ids or [])
        retired = 0
        for row in self.posts.values():
            if row["message_id"] in seen:
                if row["is_deleted"]:
                    row["is_deleted"] = False
                    row["deleted_at"] = None
                continue
            if row["is_deleted"]:
                continue
            if min_message_id is not None and row["message_id"] < min_message_id:
                continue
            row["is_deleted"] = True
            row["deleted_at"] = datetime.now(timezone.utc)
            retired += 1
        return retired

    async def latest_stats(self, channel_id=None, **kwargs):
        include_deleted = kwargs.get("include_deleted", False)
        return [
            {"message_id": row["message_id"]}
            for row in self.posts.values()
            if include_deleted or not row["is_deleted"]
        ]


def _collector_settings() -> Settings:
    return Settings(
        api_id=1,
        api_hash="hash",
        session_string="session",
        channels="@a",
        admin_password="strong-password",
        track_days=0,
        backfill_limit=0,
        poll_minutes=15,
    )


@pytest.mark.asyncio
async def test_deleted_post_retired_and_hidden_from_latest_stats():
    db = FakeDB()
    collector = Collector(FakeClient([]), db, _collector_settings())
    now = datetime.now(timezone.utc)
    first = [FakeMsg(1, now), FakeMsg(2, now)]
    collector.client = FakeClient(first)
    channel = {"id": 1, "identifier": "@a"}
    await collector.poll_channel(channel)
    assert len(db.posts) == 2

    collector.client = FakeClient([FakeMsg(1, now)])
    collector._last_full_scan.pop(1, None)
    await collector.poll_channel(channel)
    retired = [row for row in db.posts.values() if row["is_deleted"]]
    assert len(retired) == 1
    assert retired[0]["message_id"] == 2
    assert retired[0]["deleted_at"] is not None
    visible = await db.latest_stats(channel_id=1)
    assert [row["message_id"] for row in visible] == [1]


@pytest.mark.asyncio
async def test_capped_whole_history_scan_does_not_retire_older_posts():
    db = FakeDB()
    now = datetime.now(timezone.utc)
    # The archived post below the scan window has a smaller message id than
    # anything the capped scan could reach (T33 bounds retire only
    # message_id >= min(seen)).
    db.posts[1] = {"message_id": 0, "is_deleted": False, "deleted_at": None}
    db.posts[2] = {"message_id": 1, "is_deleted": False, "deleted_at": None}
    db._next_post_id = 3
    settings = _collector_settings().model_copy(update={"backfill_limit": 1})
    collector = Collector(FakeClient([FakeMsg(1, now)]), db, settings)

    await collector.poll_channel({"id": 1, "identifier": "@a"})

    assert not db.posts[1]["is_deleted"]
    assert 1 not in collector._last_full_scan


def test_migration_0013_has_expected_shape():
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0013_collection_integrity.py"
    assert path.is_file()
    content = path.read_text(encoding="utf-8")
    assert 'revision = "0013_collection_integrity"' in content
    assert 'down_revision = "0012_channel_system_prompts"' in content
    assert "deleted_at" in content
    assert "ix_snapshots_workspace_post_id" in content
    assert "SET LOCAL TIME ZONE" in content


@pytest.mark.asyncio
async def test_migration_0013_applies_cleanly_on_postgres():
    import os

    database_url = (
        os.environ.get("TEST_POSTGRES_URL")
        or os.environ.get("M1_POSTGRES_URL")
        or os.environ.get("M0_POSTGRES_URL")
    )
    if not database_url:
        pytest.skip("set TEST_POSTGRES_URL to run the 0013 migration proof")
    import asyncpg

    url = database_url
    for prefix in ("postgresql+asyncpg://", "postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql://" + url.removeprefix(prefix)
            break
    connection = await asyncpg.connect(url)
    try:
        await connection.execute("ALTER TABLE posts ADD COLUMN IF NOT EXISTS deleted_at timestamptz")
        column = await connection.fetchval(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='posts' AND column_name='deleted_at'"
        )
        assert column == "deleted_at"
        await connection.execute(
            "CREATE INDEX IF NOT EXISTS ix_snapshots_workspace_post_id "
            "ON snapshots (workspace_id, post_id, id DESC)"
        )
        index = await connection.fetchval(
            "SELECT indexname FROM pg_indexes WHERE indexname='ix_snapshots_workspace_post_id'"
        )
        assert index == "ix_snapshots_workspace_post_id"
        await connection.execute("DROP INDEX IF EXISTS ix_snapshots_workspace_post_id")
        await connection.execute("ALTER TABLE posts DROP COLUMN IF EXISTS deleted_at")
    finally:
        await connection.close()


def _health_app(last, tmp_path):
    from app.web.routes import create_app

    class HealthDB:
        is_postgres = True
        user_id = None
        workspace_id = None
        workspace_slug = "community"

        async def healthcheck(self):
            return None

        async def last_successful_cycle_at(self):
            return last

    settings = Settings(
        api_id=1,
        api_hash="hash",
        session_string="session",
        channels="@a",
        admin_password="strong-password",
        session_secret="test-secret-0123456789abcdef",
        data_dir=str(tmp_path),
        poll_minutes=15,
    )
    collector = SimpleNamespace(db=HealthDB())
    return create_app(collector, settings)


@pytest.mark.asyncio
async def test_healthz_degraded_without_storage_field(tmp_path):
    app = _health_app(datetime.now(timezone.utc) - timedelta(minutes=60), tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    payload = response.json()
    assert "storage" not in payload
    assert payload["status"] == "degraded"
    assert payload["last_successful_cycle_at"]


@pytest.mark.asyncio
async def test_healthz_ok_for_fresh_cycle(tmp_path):
    app = _health_app(datetime.now(timezone.utc), tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_prune_collection_jobs_deletes_only_old_finished():
    from app.postgres_db import PostgresDatabase

    seen: dict = {}

    class FakeResult:
        rowcount = 3

    async def fake_execute(statement, params=None):
        seen["statement"] = statement
        seen["params"] = params
        return FakeResult()

    db = PostgresDatabase.__new__(PostgresDatabase)
    db._execute = fake_execute  # type: ignore[attr-defined]
    removed = await PostgresDatabase.prune_collection_jobs(db, older_than_days=7)
    assert removed == 3
    assert "DELETE FROM collection_jobs" in seen["statement"]
    assert "status <> 'running'" in seen["statement"]
    assert seen["params"] == {"days": 7}


@pytest.mark.asyncio
async def test_claim_overlap_returns_none_while_other_errors_raise():
    from sqlalchemy.exc import IntegrityError

    from app.postgres_db import PostgresDatabase

    class Origin:
        pgcode = "23505"

    db = PostgresDatabase.__new__(PostgresDatabase)
    db._workspace = lambda: "ws"  # type: ignore[attr-defined]

    class FakeSession:
        async def execute(self, *args, **kwargs):
            raise IntegrityError("INSERT", {}, Origin())

    class FakeSessions:
        def session(self):
            class Ctx:
                async def __aenter__(self):
                    return FakeSession()

                async def __aexit__(self, *args):
                    return False

            return Ctx()

    db.sessions = FakeSessions()  # type: ignore[attr-defined]
    assert await PostgresDatabase.claim_collection_job(db, 1) is None

    class OtherOrigin:
        pgcode = "23503"

    class FailingSession:
        async def execute(self, *args, **kwargs):
            raise IntegrityError("INSERT", {}, OtherOrigin())

    class FailingSessions:
        def session(self):
            class Ctx:
                async def __aenter__(self):
                    return FailingSession()

                async def __aexit__(self, *args):
                    return False

            return Ctx()

    db.sessions = FailingSessions()  # type: ignore[attr-defined]
    with pytest.raises(IntegrityError):
        await PostgresDatabase.claim_collection_job(db, 1)
