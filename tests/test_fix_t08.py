"""T08 acceptance tests: SQLite import safety."""
from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.config import Settings
from app.migration.legacy_schema import _SCHEMA, fmt_ts


def _make_archive(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(_SCHEMA)
    connection.execute(
        "INSERT INTO channels(id, identifier, title, chat_id) VALUES (1, '@a', 'A', 1)"
    )
    connection.execute(
        "INSERT INTO posts(id, channel_id, message_id, posted_at, text) VALUES (1, 1, 10, '2024-01-01 00:00:00', 'hello')"
    )
    connection.execute(
        "INSERT INTO snapshots(id, post_id, taken_at, views, comments, reactions, shares)"
        " VALUES (1, 1, '2024-01-01 01:00:00', 1, 0, 0, 0)"
    )
    connection.commit()
    connection.close()


class _Result:
    def __init__(self, value=None, rows=None):
        self._value = value
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self._value

    def scalar_one(self):
        return self._value

    def __iter__(self):
        return iter(self._rows)


class _FakeSession:
    def __init__(self, existing_counts: dict, spy: dict):
        self.existing_counts = existing_counts
        self.spy = spy
        self.executed: list[str] = []

    async def execute(self, sql, params=None):
        text_sql = str(sql)
        self.executed.append(text_sql)
        if "to_regclass" in text_sql:
            return _Result("workspaces")
        if text_sql.strip().startswith("SELECT COUNT(*)"):
            for table in ("channels", "posts", "snapshots", "comments"):
                if f'FROM "{table}"' in text_sql:
                    return _Result(self.existing_counts.get(table, 0))
            return _Result(0)
        if "INSERT INTO" in text_sql:
            self.spy["inserts"] += 1
            if "RETURNING" in text_sql:
                return _Result(1)
            return _Result(None, [])
        if text_sql.strip().startswith("SELECT id FROM channels"):
            return _Result(1)
        if text_sql.strip().startswith("SELECT id FROM posts"):
            return _Result(1)
        if text_sql.strip().startswith("SELECT ") and "ORDER BY id" in text_sql:
            return _Result(None, [])
        if "SELECT setval" in text_sql:
            return _Result(None, [])
        return _Result(None, [])

    async def flush(self):
        return None

    async def commit(self):
        self.spy["commits"] += 1

    async def rollback(self):
        self.spy["rollbacks"] += 1


class _FakeManager:
    def __init__(self, existing_counts: dict, spy: dict):
        self.existing_counts = existing_counts
        self.spy = spy
        self.session_obj = _FakeSession(existing_counts, spy)

    def session(self):
        session_obj = self.session_obj

        class _Ctx:
            async def __aenter__(self):
                return session_obj

            async def __aexit__(self, *args):
                return False

        return _Ctx()

    async def dispose(self):
        return None


def _patch_manager(monkeypatch, existing_counts: dict, spy: dict):
    import app.migration.sqlite_to_postgres as importer

    def _factory(*args, **kwargs):
        return _FakeManager(existing_counts, spy)

    monkeypatch.setattr(importer, "DatabaseSessionManager", _factory)


def test_import_refuses_nonempty_workspace_before_any_write(tmp_path, monkeypatch):
    from app.migration import sqlite_to_postgres as importer

    archive = tmp_path / "archive.db"
    _make_archive(archive)
    spy = {"inserts": 0, "commits": 0, "rollbacks": 0}
    _patch_manager(monkeypatch, {"channels": 1, "posts": 5, "snapshots": 5, "comments": 0}, spy)
    with pytest.raises(RuntimeError, match="already contains rows"):
        asyncio.run(importer.import_sqlite(archive, "postgresql+asyncpg://u:p@h/db", workspace_slug="community"))
    assert spy["inserts"] == 0


def test_import_hash_mismatch_rolls_back_to_empty(tmp_path, monkeypatch):
    from app.migration import sqlite_to_postgres as importer

    archive = tmp_path / "archive.db"
    _make_archive(archive)
    spy = {"inserts": 0, "commits": 0, "rollbacks": 0}
    _patch_manager(monkeypatch, {"channels": 0, "posts": 0, "snapshots": 0, "comments": 0}, spy)
    # Corrupt the reconciliation by making target reads return nothing: the
    # fake session returns empty target rows, so counts differ from source.
    with pytest.raises(RuntimeError):
        asyncio.run(importer.import_sqlite(archive, "postgresql+asyncpg://u:p@h/db", workspace_slug="community"))
    assert spy["rollbacks"] >= 1
    assert spy["commits"] == 0


@pytest.mark.integration
def test_import_remaps_ids_and_rejects_same_count_changed_archive(tmp_path):
    database_url = os.getenv("TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("set TEST_POSTGRES_URL to run isolated PostgreSQL importer proof")
    from uuid import uuid4
    from sqlalchemy import text
    from app.db_session import DatabaseSessionManager
    from app.migration.sqlite_to_postgres import import_sqlite

    async def prove():
        first = tmp_path / "first.db"
        second = tmp_path / "second.db"
        _make_archive(first)
        _make_archive(second)
        connection = sqlite3.connect(second)
        connection.execute("UPDATE channels SET identifier='@b', title='B' WHERE id=1")
        connection.execute("UPDATE posts SET text='second archive' WHERE id=1")
        connection.commit()
        connection.close()
        slug_a, slug_b = f"import-a-{uuid4().hex}", f"import-b-{uuid4().hex}"
        result_a = await import_sqlite(first, database_url, workspace_slug=slug_a)
        result_b = await import_sqlite(second, database_url, workspace_slug=slug_b)
        assert result_a["all_match"] and result_b["all_match"]
        assert (await import_sqlite(second, database_url, workspace_slug=slug_b))["all_match"]
        manager = DatabaseSessionManager(database_url)
        try:
            async with manager.session() as session:
                rows = (await session.execute(text(
                    "SELECT c.id, c.identifier, p.id, p.text FROM channels c "
                    "JOIN posts p ON p.channel_id=c.id WHERE c.workspace_id IN (:a, :b) ORDER BY c.identifier"
                ), {"a": result_a["workspace_id"], "b": result_b["workspace_id"]})).all()
            assert len(rows) == 2
            assert rows[0][0] != rows[1][0] and rows[0][2] != rows[1][2]
            assert [(row[1], row[3]) for row in rows] == [("@a", "hello"), ("@b", "second archive")]
        finally:
            await manager.dispose()
        connection = sqlite3.connect(second)
        connection.execute("UPDATE posts SET text='changed after import' WHERE id=1")
        connection.commit()
        connection.close()
        with pytest.raises(RuntimeError, match="already contains rows"):
            await import_sqlite(second, database_url, workspace_slug=slug_b)
        merged = await import_sqlite(second, database_url, workspace_slug=slug_b, force=True)
        assert merged["all_match"]
        manager = DatabaseSessionManager(database_url)
        try:
            async with manager.session() as session:
                rows = (await session.execute(text(
                    "SELECT c.identifier, p.text FROM channels c JOIN posts p ON p.channel_id=c.id "
                    "WHERE c.workspace_id IN (:a, :b) ORDER BY c.identifier"
                ), {"a": result_a["workspace_id"], "b": result_b["workspace_id"]})).all()
            assert rows == [("@a", "hello"), ("@b", "changed after import")]
        finally:
            await manager.dispose()

    asyncio.run(prove())


def test_diagnostic_read_only_does_not_init(tmp_path):
    import scripts.restore_history as restore
    from app.config import Settings as S

    archive = tmp_path / "stats.db"
    _make_archive(archive)
    connection = sqlite3.connect(archive)
    before_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    connection.close()
    before = archive.stat()
    before_bytes = archive.read_bytes()
    settings = S(
        _env_file=None,
        data_dir=str(tmp_path),
        database_url="",
        api_id=1,
        api_hash="h",
        session_string="s",
        channels="@a",
        admin_password="x",
    )
    result = restore._diagnostic(settings)
    after = archive.stat()
    assert result["storage"] == "sqlite"
    assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
    assert archive.read_bytes() == before_bytes
    connection = sqlite3.connect(archive)
    after_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    connection.close()
    assert before_mode == after_mode


def test_main_exits_without_database_url(monkeypatch):
    from app.config import Settings

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("API_ID", "1")
    monkeypatch.setenv("API_HASH", "hash")
    monkeypatch.setenv("SESSION_STRING", "session")
    monkeypatch.setenv("CHANNELS", "@a")
    monkeypatch.setenv("ADMIN_PASSWORD", "strong-password")
    settings = Settings(_env_file=None)
    assert not settings.postgres_enabled
    assert "allow_legacy_sqlite" not in Settings.model_fields
    problems = settings.validate_required()
    assert any("DATABASE_URL is required (PostgreSQL)" in p for p in problems)


def test_fmt_ts_naive_is_utc_independent_of_tz(monkeypatch):
    naive = datetime(2024, 5, 1, 12, 0, 0)
    expected = fmt_ts(naive.replace(tzinfo=timezone.utc))
    for tz in ("UTC", "America/New_York", "Australia/Sydney"):
        monkeypatch.setenv("TZ", tz)
        try:
            time.tzset()
        except AttributeError:
            pass
        assert fmt_ts(naive) == expected


def test_timestamp_parses_offset_without_overwrite():
    from app.migration.sqlite_to_postgres import _timestamp

    assert _timestamp("2024-01-01T12:00:00+02:00") == datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
    assert _timestamp("2024-01-01 00:00:00") == datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
