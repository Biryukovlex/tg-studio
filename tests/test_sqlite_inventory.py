import json
import sqlite3

from app.db import _SCHEMA
from app.migration.sqlite_inventory import build_inventory, main


def _seed_archive(path, message_text="private post text"):
    connection = sqlite3.connect(path)
    connection.executescript(_SCHEMA)
    connection.execute(
        "INSERT INTO channels(identifier, title, chat_id) VALUES (?, ?, ?)",
        ("@private_channel", "Private title", 123),
    )
    connection.execute(
        """INSERT INTO posts(channel_id, message_id, posted_at, text)
           VALUES (1, 7, '2024-02-03 04:05:06', ?)""",
        (message_text,),
    )
    connection.execute(
        """INSERT INTO snapshots(post_id, taken_at, views, comments, reactions, shares)
           VALUES (1, '2024-02-03 05:05:06', 10, 2, 3, 1)"""
    )
    connection.execute(
        """INSERT INTO comments(
               post_id, telegram_message_id, discussion_chat_id,
               posted_at, text, first_collected_at, last_collected_at, last_seen_sync
           ) VALUES (1, 8, 456, '2024-02-03 06:05:06', ?,
                     '2024-02-03 06:05:06', '2024-02-03 06:05:06', 'sync-1')""",
        ("private comment text",),
    )
    connection.commit()
    connection.close()


def _tables(report):
    return {table["name"]: table for table in report["tables"]}


def test_inventory_is_read_only_aggregate_metadata(tmp_path):
    db_path = tmp_path / "stats.db"
    _seed_archive(db_path)
    before = db_path.read_bytes()

    report = build_inventory(db_path)

    assert db_path.read_bytes() == before
    tables = _tables(report)
    assert tables["channels"]["row_count"] == 1
    assert tables["posts"]["row_count"] == 1
    assert tables["snapshots"]["row_count"] == 1
    assert tables["comments"]["row_count"] == 1
    assert tables["posts"]["timestamp_ranges"]["posted_at"] == {
        "min": "2024-02-03 04:05:06",
        "max": "2024-02-03 04:05:06",
    }
    assert report["per_channel"] == [
        {
            "channel_db_id": 1,
            "post_count": 1,
            "snapshot_count": 1,
            "comment_count": 1,
        }
    ]
    assert report["integrity"] == {
        "sqlite_integrity_check_ok": True,
        "foreign_key_violation_count": 0,
    }

    # The report may hash row values, but it must not contain channel/post or
    # comment content that could be committed or shared accidentally.
    serialized = json.dumps(report, ensure_ascii=False)
    assert "private post text" not in serialized
    assert "private comment text" not in serialized
    assert "@private_channel" not in serialized
    assert "Private title" not in serialized


def test_inventory_is_reproducible_for_unchanged_archive(tmp_path):
    db_path = tmp_path / "stats.db"
    _seed_archive(db_path)

    assert build_inventory(db_path) == build_inventory(db_path)


def test_inventory_cli_writes_json_without_exposing_row_content(tmp_path):
    db_path = tmp_path / "stats.db"
    output_path = tmp_path / "inventory.json"
    _seed_archive(db_path, message_text="cli-only private body")

    assert main(["--db", str(db_path), "--output", str(output_path)]) == 0
    payload = output_path.read_text(encoding="utf-8")
    parsed = json.loads(payload)

    assert parsed["report_version"] == 1
    assert "cli-only private body" not in payload
