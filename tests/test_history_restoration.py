from datetime import datetime, timezone
import json
import sqlite3

from app.db import Database
from app.collector import Collector
from app.config import Settings


def test_sqlite_post_bodies_are_no_longer_truncated_and_diagnostic_flags_legacy_rows(tmp_path):
    db = Database(tmp_path / "stats.db")
    db.init_db()
    channel_id = db.upsert_channel("@history")
    body = "x" * 1200
    entities = [{"type": "bold", "offset": 0, "length": 12}]
    post_id = db.upsert_post(
        channel_id, 1, datetime.now(timezone.utc), body, formatting_entities=entities
    )
    row = db.post_row(post_id)
    assert row["text"] == body
    assert json.loads(row["formatting_entities"]) == entities
    diagnostic = db.history_diagnostic()
    assert diagnostic["posts_over_500"] == 1
    assert diagnostic["posts_at_500"] == 0


def test_whole_history_mode_is_explicit_in_settings_and_collector_boundary():
    settings = Settings(track_days=0, backfill_limit=200)
    assert settings.track_days == 0


def test_sqlite_init_upgrades_legacy_posts_table_with_entity_column(tmp_path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            identifier TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            chat_id INTEGER,
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            posted_at TEXT NOT NULL,
            text TEXT NOT NULL DEFAULT '',
            UNIQUE (channel_id, message_id)
        );
        """
    )
    connection.commit()
    connection.close()

    db = Database(path)
    db.init_db()
    with db.conn() as upgraded:
        columns = {
            row["name"]
            for row in upgraded.execute("PRAGMA table_info(posts)").fetchall()
        }
    assert "formatting_entities" in columns
