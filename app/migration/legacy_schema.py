"""Legacy SQLite archive shape for the read-only importer and its fixtures.

PostgreSQL is the only TG Studio runtime.  This module preserves the
YYYY-MM-DD HH:MM:SS UTC-string helpers and the archived stats.db
table layout so the standalone importer (app/migration/*) and its tests
can read old archives without a runtime database module.
"""

from __future__ import annotations

from datetime import datetime, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    identifier  TEXT UNIQUE NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    chat_id     INTEGER,
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS posts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id  INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    message_id  INTEGER NOT NULL,
    posted_at   TEXT NOT NULL,
    text        TEXT NOT NULL DEFAULT '',
    formatting_entities TEXT NOT NULL DEFAULT '[]',
    UNIQUE (channel_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_posts_channel_date ON posts(channel_id, posted_at);

CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id     INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    taken_at    TEXT NOT NULL,
    views       INTEGER NOT NULL DEFAULT 0,
    comments    INTEGER NOT NULL DEFAULT 0,
    reactions   INTEGER NOT NULL DEFAULT 0,
    shares      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_snapshots_post_time ON snapshots(post_id, taken_at);

CREATE TABLE IF NOT EXISTS comments (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id               INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    telegram_message_id   INTEGER NOT NULL,
    discussion_chat_id    INTEGER NOT NULL DEFAULT 0,
    discussion_username   TEXT NOT NULL DEFAULT '',
    sender_id             INTEGER,
    sender_name           TEXT NOT NULL DEFAULT '',
    sender_username       TEXT NOT NULL DEFAULT '',
    posted_at             TEXT NOT NULL,
    edited_at             TEXT,
    text                  TEXT NOT NULL DEFAULT '',
    media_type            TEXT NOT NULL DEFAULT '',
    reactions             INTEGER NOT NULL DEFAULT 0,
    reply_to_message_id   INTEGER,
    is_deleted            INTEGER NOT NULL DEFAULT 0,
    first_collected_at    TEXT NOT NULL,
    last_collected_at     TEXT NOT NULL,
    last_seen_sync        TEXT NOT NULL,
    UNIQUE (post_id, telegram_message_id)
);
CREATE INDEX IF NOT EXISTS idx_comments_post_date ON comments(post_id, posted_at);
CREATE INDEX IF NOT EXISTS idx_comments_sender ON comments(sender_id);
"""

# latest snapshot per post + a "24h ago" baseline for the delta column
_LATEST_CTE = """
WITH latest AS (
    SELECT s.* FROM snapshots s
    JOIN (SELECT post_id, MAX(id) AS mid FROM snapshots GROUP BY post_id) m ON m.mid = s.id
),
dayago AS (
    SELECT s.post_id, s.views FROM snapshots s
    JOIN (SELECT post_id, MAX(id) AS mid FROM snapshots
          WHERE taken_at <= datetime('now', '-24 hours') GROUP BY post_id) m ON m.mid = s.id
),
firstsnap AS (
    SELECT s.post_id, s.views FROM snapshots s
    JOIN (SELECT post_id, MIN(id) AS mid FROM snapshots GROUP BY post_id) m ON m.mid = s.id
)
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def fmt_ts(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
