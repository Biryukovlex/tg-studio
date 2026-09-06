"""SQLite storage. All timestamps are UTC strings in `YYYY-MM-DD HH:MM:SS` format
so SQLite datetime() functions work directly on them."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from .telegram_formatting import normalize_entities

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
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class Database:
    def __init__(self, db_path: str | Path) -> None:
        self.path: str = str(db_path)

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def init_db(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.executescript(_SCHEMA)
            # Existing local archives predate rich-text preservation.  Keep
            # startup backward-compatible while PostgreSQL uses Alembic.
            columns = {
                str(row["name"])
                for row in c.execute("PRAGMA table_info(posts)").fetchall()
            }
            if "formatting_entities" not in columns:
                c.execute(
                    "ALTER TABLE posts ADD COLUMN formatting_entities TEXT NOT NULL DEFAULT '[]'"
                )

    # ---------- channels ----------

    def upsert_channel(self, identifier: str, title: str = "", chat_id: int | None = None) -> int:
        with self.conn() as c:
            c.execute(
                """INSERT INTO channels(identifier, title, chat_id) VALUES(?,?,?)
                   ON CONFLICT(identifier) DO UPDATE SET
                     title=CASE WHEN excluded.title!='' THEN excluded.title ELSE channels.title END,
                     chat_id=COALESCE(excluded.chat_id, channels.chat_id)""",
                (identifier, title or "", chat_id),
            )
            row = c.execute("SELECT id FROM channels WHERE identifier=?", (identifier,)).fetchone()
            return int(row["id"])

    def get_channels(self) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute("SELECT * FROM channels WHERE active=1 ORDER BY id").fetchall()

    # ---------- posts / snapshots ----------

    def upsert_post(
        self,
        channel_db_id: int,
        message_id: int,
        posted_at: datetime,
        text: str,
        formatting_entities: Any = None,
    ) -> int:
        serialized_entities = json.dumps(
            normalize_entities(formatting_entities),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self.conn() as c:
            c.execute(
                """INSERT INTO posts(
                       channel_id, message_id, posted_at, text, formatting_entities
                   ) VALUES(?,?,?,?,?)
                   ON CONFLICT(channel_id, message_id) DO UPDATE SET
                       posted_at=excluded.posted_at,
                       text=excluded.text,
                       formatting_entities=excluded.formatting_entities""",
                # Keep the complete Telegram body.  Older releases wrote only
                # the first 500 characters; the history restoration command
                # re-fetches those rows after this cap is removed.
                (channel_db_id, message_id, fmt_ts(posted_at), text or "", serialized_entities),
            )
            row = c.execute(
                "SELECT id FROM posts WHERE channel_id=? AND message_id=?",
                (channel_db_id, message_id),
            ).fetchone()
            return int(row["id"])

    def last_snapshot(self, post_id: int) -> Optional[sqlite3.Row]:
        with self.conn() as c:
            return c.execute(
                "SELECT * FROM snapshots WHERE post_id=? ORDER BY id DESC LIMIT 1", (post_id,)
            ).fetchone()

    def add_snapshot_if_changed(
        self, post_id: int, views: int, comments: int, reactions: int, shares: int
    ) -> bool:
        """Insert only when at least one metric changed - keeps the series compact."""
        last = self.last_snapshot(post_id)
        if last is not None and (
            last["views"] == views
            and last["comments"] == comments
            and last["reactions"] == reactions
            and last["shares"] == shares
        ):
            return False
        with self.conn() as c:
            c.execute(
                "INSERT INTO snapshots(post_id, taken_at, views, comments, reactions, shares)"
                " VALUES(?,?,?,?,?,?)",
                (post_id, fmt_ts(utcnow()), views, comments, reactions, shares),
            )
        return True

    # ---------- comments ----------

    def upsert_comment(
        self,
        *,
        post_id: int,
        telegram_message_id: int,
        discussion_chat_id: int,
        discussion_username: str,
        sender_id: int | None,
        sender_name: str,
        sender_username: str,
        posted_at: datetime,
        edited_at: datetime | None,
        text: str,
        media_type: str,
        reactions: int,
        reply_to_message_id: int | None,
        sync_token: str,
    ) -> bool:
        """Insert or update one comment. Return True when stored content changed."""
        posted = fmt_ts(posted_at)
        edited = fmt_ts(edited_at) if edited_at else None
        now = fmt_ts(utcnow())
        values = (
            discussion_chat_id,
            discussion_username or "",
            sender_id,
            sender_name or "",
            sender_username or "",
            posted,
            edited,
            text or "",
            media_type or "",
            reactions,
            reply_to_message_id,
        )
        with self.conn() as c:
            existing = c.execute(
                """SELECT discussion_chat_id, discussion_username, sender_id,
                          sender_name, sender_username, posted_at, edited_at,
                          text, media_type, reactions, reply_to_message_id, is_deleted
                     FROM comments
                    WHERE post_id=? AND telegram_message_id=?""",
                (post_id, telegram_message_id),
            ).fetchone()
            changed = existing is None or tuple(existing) != values + (0,)
            c.execute(
                """INSERT INTO comments(
                       post_id, telegram_message_id, discussion_chat_id,
                       discussion_username, sender_id, sender_name, sender_username,
                       posted_at, edited_at, text, media_type, reactions,
                       reply_to_message_id, is_deleted, first_collected_at,
                       last_collected_at, last_seen_sync
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?)
                   ON CONFLICT(post_id, telegram_message_id) DO UPDATE SET
                       discussion_chat_id=excluded.discussion_chat_id,
                       discussion_username=excluded.discussion_username,
                       sender_id=excluded.sender_id,
                       sender_name=excluded.sender_name,
                       sender_username=excluded.sender_username,
                       posted_at=excluded.posted_at,
                       edited_at=excluded.edited_at,
                       text=excluded.text,
                       media_type=excluded.media_type,
                       reactions=excluded.reactions,
                       reply_to_message_id=excluded.reply_to_message_id,
                       is_deleted=0,
                       last_collected_at=excluded.last_collected_at,
                       last_seen_sync=excluded.last_seen_sync""",
                (
                    post_id,
                    telegram_message_id,
                    *values,
                    now,
                    now,
                    sync_token,
                ),
            )
            return changed

    def mark_unseen_comments_deleted(self, post_id: int, sync_token: str) -> int:
        """Soft-delete comments absent from a successfully completed thread scan."""
        with self.conn() as c:
            cur = c.execute(
                """UPDATE comments
                      SET is_deleted=1, last_collected_at=?
                    WHERE post_id=? AND is_deleted=0 AND last_seen_sync<>?""",
                (fmt_ts(utcnow()), post_id, sync_token),
            )
            return int(cur.rowcount)

    def has_comments(self, post_id: int) -> bool:
        with self.conn() as c:
            row = c.execute(
                "SELECT 1 FROM comments WHERE post_id=? LIMIT 1", (post_id,)
            ).fetchone()
            return row is not None

    def comments_for_post(self, post_id: int) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute(
                """SELECT * FROM comments
                    WHERE post_id=?
                    ORDER BY posted_at ASC, telegram_message_id ASC""",
                (post_id,),
            ).fetchall()

    def all_comments(self, channel_id: int | None = None) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute(
                """SELECT cm.*, p.message_id AS post_message_id, p.posted_at AS post_posted_at,
                          ch.identifier AS channel_identifier
                     FROM comments cm
                     JOIN posts p ON p.id=cm.post_id
                     JOIN channels ch ON ch.id=p.channel_id
                    WHERE (? IS NULL OR p.channel_id=?)
                    ORDER BY cm.posted_at DESC, cm.id DESC""",
                (channel_id, channel_id),
            ).fetchall()

    # ---------- queries for admin panel / commands ----------

    _ORDER_SQL = {
        "date": "p.posted_at DESC",
        "views": "l.views DESC",
        "reactions": "l.reactions DESC",
        "comments": "l.comments DESC",
        "shares": "l.shares DESC",
    }

    def latest_stats(
        self, channel_id: int | None = None, limit: int = 500, order: str = "date"
    ) -> list[sqlite3.Row]:
        sql = _LATEST_CTE + f"""
        SELECT p.id, p.message_id, p.posted_at, p.text, p.channel_id,
               c.identifier, c.title AS channel_title, c.chat_id,
               COALESCE(l.views,0) AS views, COALESCE(l.comments,0) AS comments,
               COALESCE(l.reactions,0) AS reactions, COALESCE(l.shares,0) AS shares,
               (SELECT COUNT(*) FROM comments cm
                 WHERE cm.post_id=p.id AND cm.is_deleted=0) AS collected_comments,
               l.taken_at AS updated_at,
               l.views - COALESCE(d.views, f.views, l.views) AS views_delta
        FROM posts p
        JOIN channels c ON c.id = p.channel_id
        LEFT JOIN latest l ON l.post_id = p.id
        LEFT JOIN dayago d ON d.post_id = p.id
        LEFT JOIN firstsnap f ON f.post_id = p.id
        WHERE (? IS NULL OR p.channel_id = ?)
        ORDER BY {self._ORDER_SQL.get(order, 'p.posted_at DESC')}
        LIMIT ?
        """
        with self.conn() as c:
            return c.execute(sql, (channel_id, channel_id, limit)).fetchall()

    def kpis(self, channel_id: int | None = None) -> dict[str, Any]:
        sql = _LATEST_CTE + """
        SELECT COUNT(*) AS posts,
               COALESCE(SUM(l.views),0) AS views,
               COALESCE(SUM(l.comments),0) AS comments,
               COALESCE(SUM(l.reactions),0) AS reactions,
               COALESCE(SUM(l.shares),0) AS shares,
               (SELECT COUNT(*) FROM comments cm
                  JOIN posts cp ON cp.id=cm.post_id
                 WHERE cm.is_deleted=0 AND (? IS NULL OR cp.channel_id=?)) AS collected_comments,
               (SELECT MAX(s.taken_at) FROM snapshots s
                  JOIN posts pp ON pp.id = s.post_id
                 WHERE (? IS NULL OR pp.channel_id = ?)) AS last_poll
        FROM posts p
        LEFT JOIN latest l ON l.post_id = p.id
        WHERE (? IS NULL OR p.channel_id = ?)
        """
        with self.conn() as c:
            r = c.execute(
                sql,
                (channel_id, channel_id, channel_id, channel_id, channel_id, channel_id),
            ).fetchone()
            return dict(r) if r else {}

    def post_row(self, post_id: int) -> Optional[sqlite3.Row]:
        sql = _LATEST_CTE + """
        SELECT p.*, c.identifier, c.title AS channel_title, c.chat_id,
               COALESCE(l.views,0) AS views, COALESCE(l.comments,0) AS comments,
               COALESCE(l.reactions,0) AS reactions, COALESCE(l.shares,0) AS shares,
               (SELECT COUNT(*) FROM comments cm
                 WHERE cm.post_id=p.id AND cm.is_deleted=0) AS collected_comments
        FROM posts p
        JOIN channels c ON c.id = p.channel_id
        LEFT JOIN latest l ON l.post_id = p.id
        WHERE p.id = ?
        """
        with self.conn() as c:
            return c.execute(sql, (post_id,)).fetchone()

    def post_history(self, post_id: int) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute(
                "SELECT * FROM snapshots WHERE post_id=? ORDER BY taken_at ASC, id ASC",
                (post_id,),
            ).fetchall()

    def history_diagnostic(self, channel_id: int | None = None) -> dict[str, Any]:
        """Return safe counts/ranges for planning a full-history refresh."""
        with self.conn() as c:
            row = c.execute(
                """SELECT COUNT(*) AS total_posts,
                          SUM(CASE WHEN length(p.text) > 500 THEN 1 ELSE 0 END) AS posts_over_500,
                          SUM(CASE WHEN length(p.text) = 500 THEN 1 ELSE 0 END) AS posts_at_500,
                          MIN(p.posted_at) AS oldest_post, MAX(p.posted_at) AS newest_post,
                          COUNT(DISTINCT p.channel_id) AS channels
                     FROM posts p
                    WHERE (? IS NULL OR p.channel_id=?)""",
                (channel_id, channel_id),
            ).fetchone()
            channels = c.execute(
                """SELECT p.channel_id, c.identifier, COUNT(*) AS posts_at_500
                     FROM posts p JOIN channels c ON c.id=p.channel_id
                    WHERE p.text IS NOT NULL AND length(p.text)=500
                      AND (? IS NULL OR p.channel_id=?)
                    GROUP BY p.channel_id, c.identifier ORDER BY p.channel_id""",
                (channel_id, channel_id),
            ).fetchall()
        return {
            "total_posts": int(row["total_posts"] or 0),
            "posts_over_500": int(row["posts_over_500"] or 0),
            "posts_at_500": int(row["posts_at_500"] or 0),
            "oldest_post": row["oldest_post"],
            "newest_post": row["newest_post"],
            "channels": int(row["channels"] or 0),
            "channels_requiring_recollection": [dict(item) for item in channels],
        }

    def timeseries_totals(
        self, days: int | None, channel_id: int | None = None
    ) -> dict[str, list]:
        """Daily buckets over the last `days` days.

        Returns cumulative totals of each metric across all tracked posts as known
        at the end of each day, plus how many posts were published per day.
        """
        with self.conn() as c:
            if days is None:
                first = c.execute(
                    """SELECT MIN(substr(posted_at,1,10)) AS day FROM posts
                        WHERE (? IS NULL OR channel_id=?)""",
                    (channel_id, channel_id),
                ).fetchone()
                start_day = (first["day"] if first else None) or utcnow().strftime("%Y-%m-%d")
                start_date = datetime.strptime(start_day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            else:
                start_date = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
                start_date -= timedelta(days=max(1, days) - 1)
            since = fmt_ts(start_date)
            span = max(1, (utcnow().date() - start_date.date()).days + 1)
            days_list = [
                (start_date + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(span)
            ]

            rows = c.execute(
                """
                SELECT p.channel_id AS ch, s.taken_at, s.post_id, s.views, s.comments, s.reactions, s.shares
                FROM snapshots s
                JOIN posts p ON p.id = s.post_id
                WHERE (? IS NULL OR p.channel_id = ?)
                ORDER BY s.taken_at ASC, s.id ASC
                """,
                (channel_id, channel_id),
            ).fetchall()
            new_posts = {
                r["day"]: r["n"]
                for r in c.execute(
                    """SELECT substr(posted_at,1,10) AS day, COUNT(*) AS n FROM posts
                       WHERE (? IS NULL OR channel_id = ?) AND posted_at >= ?
                       GROUP BY day""",
                    (channel_id, channel_id, since),
                ).fetchall()
            }

        # replay snapshot history into daily buckets
        state: dict[int, tuple[int, int, int, int]] = {}
        by_day: dict[str, list[sqlite3.Row]] = {}
        for r in rows:
            day = r["taken_at"][:10]
            if day < days_list[0]:
                state[r["post_id"]] = (r["views"], r["comments"], r["reactions"], r["shares"])
            else:
                by_day.setdefault(day, []).append(r)

        result_views: list[int] = []
        result_comments: list[int] = []
        result_reactions: list[int] = []
        result_shares: list[int] = []
        posts_per_day: list[int] = []

        for day in days_list:
            for r in sorted(by_day.get(day, []), key=lambda x: x["taken_at"]):
                state[r["post_id"]] = (r["views"], r["comments"], r["reactions"], r["shares"])
            vals = list(state.values()) or [(0, 0, 0, 0)]
            result_views.append(sum(v[0] for v in vals))
            result_comments.append(sum(v[1] for v in vals))
            result_reactions.append(sum(v[2] for v in vals))
            result_shares.append(sum(v[3] for v in vals))
            posts_per_day.append(new_posts.get(day, 0))

        return {
            "days": days_list,
            "views": result_views,
            "comments": result_comments,
            "reactions": result_reactions,
            "shares": result_shares,
            "posts_per_day": posts_per_day,
        }
