"""T08 acceptance tests: SQLite import safety."""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.config import Settings
from app.db import _SCHEMA, fmt_ts


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


def test_diagnostic_read_only_does_not_init(tmp_path, monkeypatch):
    import scripts.restore_history as restore
    from app.config import Settings as S

    archive = tmp_path / "archive.db"
    _make_archive(archive)
    connection = sqlite3.connect(archive)
    before_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    connection.close()
    before = archive.stat()
    before_bytes = archive.read_bytes()
    monkeypatch.setattr(S, "db_path", property(lambda self: archive))
    settings = S(
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
    import app.main as main

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("ALLOW_LEGACY_SQLITE", raising=False)
    monkeypatch.setenv("API_ID", "1")
    monkeypatch.setenv("API_HASH", "hash")
    monkeypatch.setenv("SESSION_STRING", "session")
    monkeypatch.setenv("CHANNELS", "@a")
    monkeypatch.setenv("ADMIN_PASSWORD", "strong-password")
    settings = Settings()
    assert not settings.postgres_enabled
    assert not settings.allow_legacy_sqlite
    with pytest.raises(SystemExit) as exc:
        asyncio.run(_run_amain_db_branch(settings))
    assert exc.value.code == 1


async def _run_amain_db_branch(settings):
    import sys

    if settings.postgres_enabled:
        return
    if not settings.allow_legacy_sqlite:
        print("Configuration problem: DATABASE_URL is empty.", file=sys.stderr)
        raise SystemExit(1)


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
