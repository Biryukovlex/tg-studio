import asyncio
import os
import sqlite3
import subprocess
import sys

import pytest

from app.db import _SCHEMA
from app.migration.postgres_fixture import import_fixture

pytestmark = pytest.mark.integration


def _seed_fixture(path):
    connection = sqlite3.connect(path)
    connection.executescript(_SCHEMA)
    connection.execute(
        "INSERT INTO channels(identifier, title, chat_id) VALUES (?, ?, ?)",
        ("@fixture_channel", "Fixture channel", 1001),
    )
    connection.execute(
        """INSERT INTO posts(channel_id, message_id, posted_at, text)
           VALUES (1, 77, '2024-04-05 06:07:08', ?)""",
        ("Юникод fixture — do not use production content",),
    )
    connection.execute(
        """INSERT INTO snapshots(post_id, taken_at, views, comments, reactions, shares)
           VALUES (1, '2024-04-05 07:07:08', 11, 2, 3, 4)"""
    )
    connection.execute(
        """INSERT INTO comments(
               post_id, telegram_message_id, discussion_chat_id,
               posted_at, text, first_collected_at, last_collected_at, last_seen_sync
           ) VALUES (1, 78, 1002, '2024-04-05 08:07:08', ?,
                     '2024-04-05 08:07:08', '2024-04-05 08:07:08', 'fixture-sync')""",
        ("Комментарий fixture — do not use production content",),
    )
    connection.commit()
    connection.close()


def test_alembic_migration_and_fixture_import_reconcile(tmp_path):
    database_url = os.environ.get("M0_POSTGRES_URL")
    if not database_url:
        pytest.skip("set M0_POSTGRES_URL to run the isolated PostgreSQL proof")

    fixture_path = tmp_path / "fixture.db"
    _seed_fixture(fixture_path)
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url

    migration = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert migration.returncode == 0

    reconciliation = asyncio.run(import_fixture(fixture_path, database_url))

    assert reconciliation["all_match"] is True
    assert all(report["match"] for report in reconciliation["tables"])
    assert {report["name"] for report in reconciliation["tables"]} == {
        "channels",
        "posts",
        "snapshots",
        "comments",
    }
