"""Workspace-scoped PostgreSQL repository used by the collector and web app.

The repository deliberately exposes the same high-level operations as the
legacy SQLite ``Database`` class.  Its methods are asynchronous and return
mapping-like rows, which lets the existing templates and command formatter
continue to work while the I/O path moves to SQLAlchemy/asyncpg.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from . import limits
from .db_session import DatabaseSessionManager, normalize_database_url
from .telegram_formatting import normalize_entities


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    value = _aware(value)
    return value.isoformat(timespec="microseconds") if value else None


class PostgresDatabase:
    """Async PostgreSQL repository with an explicit workspace boundary."""

    is_postgres = True

    def __init__(
        self,
        database_url: str,
        *,
        workspace_slug: str = "community",
        pool_size: int = 5,
        max_overflow: int = 5,
        pool_timeout: float = 30.0,
        pool_recycle: int = 1800,
    ) -> None:
        self.database_url = normalize_database_url(database_url)
        self.workspace_slug = workspace_slug.strip() or "community"
        self.workspace_id: uuid.UUID | None = None
        self.active_channel_count: int | None = None
        self.user_id: uuid.UUID | None = None
        self.sessions = DatabaseSessionManager(
            self.database_url,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=pool_timeout,
            pool_recycle=pool_recycle,
        )

    @classmethod
    def from_settings(cls, settings) -> "PostgresDatabase":
        return cls(
            settings.database_url,
            workspace_slug=limits.WORKSPACE_SLUG,
            pool_size=limits.DATABASE_POOL_SIZE,
            max_overflow=limits.DATABASE_MAX_OVERFLOW,
            pool_timeout=limits.DATABASE_POOL_TIMEOUT,
            pool_recycle=limits.DATABASE_POOL_RECYCLE,
        )

    async def init_db(self, *, admin_username: str = "admin", workspace_name: str = "Community") -> None:
        """Check migration readiness and seed the local workspace/admin.

        Schema creation intentionally stays in Alembic.  Starting against an
        empty database therefore fails with a precise migration instruction
        instead of silently creating a divergent production schema.
        """
        await self.sessions.healthcheck()
        async with self.sessions.session() as session:
            result = await session.execute(text("SELECT to_regclass('public.workspaces') AS table_name"))
            if result.scalar_one_or_none() is None:
                raise RuntimeError(
                    "PostgreSQL schema is not migrated; run `alembic upgrade head` before starting the app"
                )
            workspace_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f"telegram-stats:workspace:{self.workspace_slug}")
            user_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f"telegram-stats:user:{admin_username}")
            await session.execute(
                text(
                    """INSERT INTO workspaces(id, slug, name) VALUES (:id, :slug, :name)
                       ON CONFLICT (slug) DO UPDATE SET name=EXCLUDED.name"""
                ),
                {"id": workspace_uuid, "slug": self.workspace_slug, "name": workspace_name},
            )
            workspace_uuid = (
                await session.execute(
                    text("SELECT id FROM workspaces WHERE slug=:slug"), {"slug": self.workspace_slug}
                )
            ).scalar_one()
            await session.execute(
                text(
                    """INSERT INTO users(id, username, display_name)
                       VALUES (:id, :username, :display_name)
                       ON CONFLICT (username) DO UPDATE SET display_name=EXCLUDED.display_name,
                                                             is_active=true"""
                ),
                {"id": user_uuid, "username": admin_username, "display_name": admin_username},
            )
            user_uuid = (
                await session.execute(
                    text("SELECT id FROM users WHERE username=:username"), {"username": admin_username}
                )
            ).scalar_one()
            await session.execute(
                text(
                    """INSERT INTO memberships(workspace_id, user_id, role)
                       VALUES (:workspace_id, :user_id, 'owner')
                       ON CONFLICT (workspace_id, user_id) DO UPDATE SET role='owner'"""
                ),
                {"workspace_id": workspace_uuid, "user_id": user_uuid},
            )
            await session.commit()
            self.workspace_id = workspace_uuid
            self.user_id = user_uuid

    async def ensure_workspace(self, *, admin_username: str = "admin") -> uuid.UUID:
        """Seed/read the community workspace after a test-created schema."""
        if self.workspace_id is None:
            await self.init_db(admin_username=admin_username)
        assert self.workspace_id is not None
        return self.workspace_id

    def _workspace(self) -> uuid.UUID:
        if self.workspace_id is None:
            raise RuntimeError("PostgresDatabase.init_db() must run before repository access")
        return self.workspace_id

    async def close(self) -> None:
        await self.sessions.dispose()

    async def healthcheck(self) -> None:
        await self.sessions.healthcheck()

    async def workspace_context(self, *, username: str | None = None) -> dict[str, Any]:
        """Return the seeded workspace/user/membership tuple for auth wiring."""
        params: dict[str, Any] = {"slug": self.workspace_slug}
        username_filter = ""
        if username:
            username_filter = " AND u.username=:username"
            params["username"] = username
        result = await self._execute(
            f"""SELECT w.id AS workspace_id, w.slug AS workspace_slug,
                       u.id AS user_id, u.username, m.role
                  FROM workspaces w JOIN memberships m ON m.workspace_id=w.id
                  JOIN users u ON u.id=m.user_id
                 WHERE w.slug=:slug{username_filter}
                 ORDER BY u.username LIMIT 1""",
            params,
        )
        row = result.mappings().first()
        if not row:
            raise LookupError("workspace membership is not seeded")
        return dict(row)

    async def list_workspaces_for_user(self, user_id: uuid.UUID) -> list[dict[str, Any]]:
        result = await self._execute(
            """SELECT w.id AS workspace_id, w.slug AS workspace_slug, m.role
                 FROM memberships m JOIN workspaces w ON w.id=m.workspace_id
                WHERE m.user_id=:user_id ORDER BY w.slug""",
            {"user_id": user_id},
        )
        return [dict(row) for row in result.mappings().all()]

    # ---------- Telegram connection/session persistence ----------

    async def persist_telegram_session(
        self,
        *,
        label: str,
        api_id: int,
        api_hash: str,
        session_string: str,
        cipher,
    ) -> uuid.UUID:
        if cipher is None:
            raise ValueError("an external TELEGRAM_SESSION_ENCRYPTION_KEY is required for persistence")
        workspace_id = self._workspace()
        encrypted = cipher.encrypt(session_string)
        fingerprint = hashlib.sha256(session_string.encode("utf-8")).hexdigest()[:16]
        async with self.sessions.session() as session:
            row = (
                await session.execute(
                    text(
                        """INSERT INTO telegram_connections(
                               id, workspace_id, label, api_id, api_hash,
                               encrypted_session, session_key_version,
                               session_fingerprint, status, updated_at
                           ) VALUES (:id, :workspace_id, :label, :api_id, :api_hash,
                                     :encrypted_session, :key_version,
                                     :fingerprint, 'active', now())
                           ON CONFLICT (workspace_id, label) DO UPDATE SET
                               api_id=EXCLUDED.api_id, api_hash=EXCLUDED.api_hash,
                               encrypted_session=EXCLUDED.encrypted_session,
                               session_key_version=EXCLUDED.session_key_version,
                               session_fingerprint=EXCLUDED.session_fingerprint,
                               status='active', updated_at=now()
                           RETURNING id"""
                    ),
                    {
                        "id": uuid.uuid4(),
                        "workspace_id": workspace_id,
                        "label": label,
                        "api_id": api_id,
                        "api_hash": api_hash,
                        "encrypted_session": encrypted,
                        "key_version": cipher.key_version,
                        "fingerprint": fingerprint,
                    },
                )
            ).scalar_one()
            await session.commit()
            return row

    async def load_telegram_session(self, *, label: str, cipher) -> str | None:
        if cipher is None:
            return None
        row = (
            await self._execute(
                """SELECT encrypted_session FROM telegram_connections
                   WHERE workspace_id=:workspace_id AND label=:label AND status='active'""",
                {"label": label},
            )
        ).first()
        if not row or row[0] is None:
            return None
        return cipher.decrypt(bytes(row[0]))

    async def _execute(self, statement: str, params: dict[str, Any] | None = None):
        async with self.sessions.session() as session:
            result = await session.execute(text(statement), {"workspace_id": self._workspace(), **(params or {})})
            # Repository writes are short, explicit transactions.  Read-only
            # results are fully buffered by SQLAlchemy before the session is
            # closed, so callers can safely consume their mappings.
            keyword = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
            if keyword not in {"SELECT", "WITH", "SHOW", "EXPLAIN"}:
                await session.commit()
            return result

    # ---------- channels ----------

    async def upsert_channel(
        self,
        identifier: str,
        title: str = "",
        chat_id: int | None = None,
        connection_id: uuid.UUID | None = None,
    ) -> int:
        row = (
            await self._execute(
                """INSERT INTO channels(workspace_id, identifier, title, chat_id, telegram_connection_id, created_at)
                   VALUES (:workspace_id, :identifier, :title, :chat_id, :connection_id, now())
                   ON CONFLICT (workspace_id, identifier) DO UPDATE SET
                       title=CASE WHEN EXCLUDED.title <> '' THEN EXCLUDED.title ELSE channels.title END,
                       chat_id=COALESCE(EXCLUDED.chat_id, channels.chat_id),
                       telegram_connection_id=COALESCE(EXCLUDED.telegram_connection_id, channels.telegram_connection_id)
                   RETURNING id""",
                {"identifier": identifier, "title": title or "", "chat_id": chat_id, "connection_id": connection_id},
            )
        ).first()
        assert row is not None
        return int(row[0])

    async def get_channels(self) -> list[dict[str, Any]]:
        result = await self._execute(
            """SELECT id, workspace_id, identifier, title, chat_id, active
               FROM channels WHERE workspace_id=:workspace_id AND active=true ORDER BY id"""
        )
        rows = [dict(row) for row in result.mappings().all()]
        # Studio readiness reads this count synchronously; the collector and the
        # dashboard refresh it on every call, so it never goes stale for long.
        self.active_channel_count = len(rows)
        return rows

    async def add_channel(self, identifier: str) -> int:
        # Normalize identifier: strip, ensure non-empty, basic validation
        ident = identifier.strip()
        if not ident:
            raise ValueError("Channel identifier must not be empty")
        # Basic validation matching T23 spec: @name, t.me/name, or -100...
        # We keep it permissive but reject strings with spaces
        if " " in ident:
            raise ValueError("Channel identifier must not contain spaces")
        # Insert with chat_id NULL so next poll resolves it; reactivate if inactive
        row = (
            await self._execute(
                """INSERT INTO channels(workspace_id, identifier, title, chat_id, active, created_at)
                   VALUES (:workspace_id, :identifier, '', NULL, true, now())
                   ON CONFLICT (workspace_id, identifier) DO UPDATE SET active=true
                   RETURNING id""",
                {"identifier": ident},
            )
        ).first()
        assert row is not None
        await self.get_channels()
        return int(row[0])

    async def deactivate_channel(self, channel_id: int) -> bool:
        result = await self._execute(
            """UPDATE channels SET active=false
               WHERE workspace_id=:workspace_id AND id=:channel_id AND active=true""",
            {"channel_id": channel_id},
        )
        await self.get_channels()
        return bool(result.rowcount and result.rowcount > 0)

    async def telegram_connection_secrets(self, label: str, *, cipher) -> dict[str, Any] | None:
        """Server-side read of the stored connection so a partial edit can be merged.

        Never expose the result to a template or an API response.
        """
        row = (
            await self._execute(
                """SELECT api_id, api_hash, encrypted_session FROM telegram_connections
                   WHERE workspace_id=:workspace_id AND label=:label""",
                {"label": label},
            )
        ).mappings().first()
        if not row:
            return None
        session_string = None
        if row["encrypted_session"] is not None and cipher is not None:
            try:
                session_string = cipher.decrypt(bytes(row["encrypted_session"]))
            except Exception:  # noqa: BLE001 - a rotated key means the session is unusable
                session_string = None
        return {"api_id": row["api_id"], "api_hash": row["api_hash"], "session_string": session_string}

    async def telegram_connection_status(self, label: str) -> dict[str, Any]:
        row = (
            await self._execute(
                """SELECT api_id, encrypted_session, updated_at, status FROM telegram_connections
                   WHERE workspace_id=:workspace_id AND label=:label""",
                {"label": label},
            )
        ).mappings().first()
        if not row:
            return {"configured": False, "api_id": None, "has_session": False, "updated_at": None}
        d = dict(row)
        has_session = bool(d.get("encrypted_session"))
        api_id = d.get("api_id")
        updated_at = d.get("updated_at")
        configured = bool(api_id and has_session)
        return {"configured": configured, "api_id": api_id, "has_session": has_session, "updated_at": updated_at}

    # ---------- posts / snapshots ----------

    async def upsert_post(
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
        row = (
            await self._execute(
                """INSERT INTO posts(
                           workspace_id, channel_id, message_id, posted_at, text,
                           formatting_entities, is_deleted, created_at
                       )
                   VALUES (
                           :workspace_id, :channel_id, :message_id, :posted_at, :text,
                           CAST(:formatting_entities AS jsonb), false, now()
                       )
                   ON CONFLICT (workspace_id, channel_id, message_id) DO UPDATE SET
                       posted_at=EXCLUDED.posted_at,
                       text=EXCLUDED.text,
                       formatting_entities=EXCLUDED.formatting_entities
                   RETURNING id""",
                {
                    "channel_id": channel_db_id,
                    "message_id": message_id,
                    "posted_at": _aware(posted_at),
                    "text": text or "",
                    "formatting_entities": serialized_entities,
                },
            )
        ).first()
        assert row is not None
        return int(row[0])

    async def last_snapshot(self, post_id: int) -> dict[str, Any] | None:
        result = await self._execute(
            """SELECT * FROM snapshots
               WHERE workspace_id=:workspace_id AND post_id=:post_id
               ORDER BY id DESC LIMIT 1""",
            {"post_id": post_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def add_snapshot_if_changed(
        self, post_id: int, views: int, comments: int, reactions: int, shares: int
    ) -> bool:
        async with self.sessions.session() as session:
            params = {
                "workspace_id": self._workspace(),
                "post_id": post_id,
                "views": int(views or 0),
                "comments": int(comments or 0),
                "reactions": int(reactions or 0),
                "shares": int(shares or 0),
            }
            latest = (
                await session.execute(
                    text(
                        """SELECT views, comments, reactions, shares FROM snapshots
                           WHERE workspace_id=:workspace_id AND post_id=:post_id
                           ORDER BY id DESC LIMIT 1 FOR UPDATE"""
                    ),
                    params,
                )
            ).mappings().first()
            if latest and all(latest[key] == params[key] for key in ("views", "comments", "reactions", "shares")):
                await session.rollback()
                return False
            await session.execute(
                text(
                    """INSERT INTO snapshots(workspace_id, post_id, taken_at, views, comments, reactions, shares)
                       VALUES (:workspace_id, :post_id, now(), :views, :comments, :reactions, :shares)"""
                ),
                params,
            )
            await session.commit()
            return True

    # ---------- comments ----------

    async def upsert_comment(self, **kwargs: Any) -> bool:
        workspace_id = self._workspace()
        values = {
            "workspace_id": workspace_id,
            "post_id": kwargs["post_id"],
            "telegram_message_id": kwargs["telegram_message_id"],
            "discussion_chat_id": kwargs.get("discussion_chat_id", 0),
            "discussion_username": kwargs.get("discussion_username", "") or "",
            "sender_id": kwargs.get("sender_id"),
            "sender_name": kwargs.get("sender_name", "") or "",
            "sender_username": kwargs.get("sender_username", "") or "",
            "posted_at": _aware(kwargs["posted_at"]),
            "edited_at": _aware(kwargs.get("edited_at")),
            "text": kwargs.get("text", "") or "",
            "media_type": kwargs.get("media_type", "") or "",
            "reactions": kwargs.get("reactions", 0) or 0,
            "reply_to_message_id": kwargs.get("reply_to_message_id"),
            "sync_token": kwargs["sync_token"],
        }
        async with self.sessions.session() as session:
            existing = (
                await session.execute(
                    text(
                        """SELECT discussion_chat_id, discussion_username, sender_id,
                                  sender_name, sender_username, posted_at, edited_at,
                                  text, media_type, reactions, reply_to_message_id, is_deleted
                           FROM comments
                           WHERE workspace_id=:workspace_id AND post_id=:post_id
                             AND telegram_message_id=:telegram_message_id"""
                    ),
                    values,
                )
            ).mappings().first()
            changed = existing is None or existing["is_deleted"] or any(
                existing[key] != values[key]
                for key in (
                    "discussion_chat_id", "discussion_username", "sender_id", "sender_name",
                    "sender_username", "posted_at", "edited_at", "text", "media_type",
                    "reactions", "reply_to_message_id",
                )
            )
            await session.execute(
                text(
                    """INSERT INTO comments(
                           workspace_id, post_id, telegram_message_id, discussion_chat_id,
                           discussion_username, sender_id, sender_name, sender_username,
                           posted_at, edited_at, text, media_type, reactions,
                           reply_to_message_id, is_deleted, first_collected_at,
                           last_collected_at, last_seen_sync
                       ) VALUES (:workspace_id, :post_id, :telegram_message_id, :discussion_chat_id,
                                 :discussion_username, :sender_id, :sender_name, :sender_username,
                                 :posted_at, :edited_at, :text, :media_type, :reactions,
                                 :reply_to_message_id, false, now(), now(), :sync_token)
                       ON CONFLICT (workspace_id, post_id, telegram_message_id) DO UPDATE SET
                           discussion_chat_id=EXCLUDED.discussion_chat_id,
                           discussion_username=EXCLUDED.discussion_username,
                           sender_id=EXCLUDED.sender_id, sender_name=EXCLUDED.sender_name,
                           sender_username=EXCLUDED.sender_username, posted_at=EXCLUDED.posted_at,
                           edited_at=EXCLUDED.edited_at, text=EXCLUDED.text,
                           media_type=EXCLUDED.media_type, reactions=EXCLUDED.reactions,
                           reply_to_message_id=EXCLUDED.reply_to_message_id,
                           is_deleted=false, last_collected_at=now(), last_seen_sync=EXCLUDED.last_seen_sync"""
                ),
                values,
            )
            await session.commit()
            return bool(changed)

    async def mark_unseen_comments_deleted(self, post_id: int, sync_token: str) -> int:
        result = await self._execute(
            """UPDATE comments SET is_deleted=true, last_collected_at=now()
               WHERE workspace_id=:workspace_id AND post_id=:post_id AND is_deleted=false
                 AND last_seen_sync <> :sync_token""",
            {"post_id": post_id, "sync_token": sync_token},
        )
        return int(result.rowcount or 0)

    async def has_comments(self, post_id: int) -> bool:
        result = await self._execute(
            "SELECT 1 FROM comments WHERE workspace_id=:workspace_id AND post_id=:post_id LIMIT 1",
            {"post_id": post_id},
        )
        return result.first() is not None

    async def comments_for_post(self, post_id: int) -> list[dict[str, Any]]:
        result = await self._execute(
            """SELECT * FROM comments WHERE workspace_id=:workspace_id AND post_id=:post_id
               ORDER BY posted_at ASC, telegram_message_id ASC""",
            {"post_id": post_id},
        )
        return [dict(row) for row in result.mappings().all()]

    async def all_comments(self, channel_id: int | None = None) -> list[dict[str, Any]]:
        result = await self._execute(
            """SELECT cm.*, p.message_id AS post_message_id, p.posted_at AS post_posted_at,
                      ch.identifier AS channel_identifier
               FROM comments cm JOIN posts p ON p.id=cm.post_id AND p.workspace_id=cm.workspace_id
               JOIN channels ch ON ch.id=p.channel_id AND ch.workspace_id=p.workspace_id
               WHERE cm.workspace_id=:workspace_id AND (CAST(:channel_id AS bigint) IS NULL OR p.channel_id=CAST(:channel_id AS bigint))
               ORDER BY cm.posted_at DESC, cm.id DESC""",
            {"channel_id": channel_id},
        )
        return [dict(row) for row in result.mappings().all()]

    # ---------- dashboard/command queries ----------

    _ORDER_SQL = {
        "date": "p.posted_at DESC",
        "views": "l.views DESC",
        "reactions": "l.reactions DESC",
        "comments": "l.comments DESC",
        "shares": "l.shares DESC",
    }

    _LATEST_CTE = """
    WITH latest AS (
        SELECT DISTINCT ON (workspace_id, post_id) * FROM snapshots
        WHERE workspace_id=:workspace_id ORDER BY workspace_id, post_id, id DESC
    ), dayago AS (
        SELECT DISTINCT ON (workspace_id, post_id) post_id, views FROM snapshots
        WHERE workspace_id=:workspace_id AND taken_at <= now() - interval '24 hours'
        ORDER BY workspace_id, post_id, id DESC
    ), firstsnap AS (
        SELECT DISTINCT ON (workspace_id, post_id) post_id, views FROM snapshots
        WHERE workspace_id=:workspace_id ORDER BY workspace_id, post_id, id ASC
    )
    """

    async def latest_stats(self, channel_id: int | None = None, limit: int = 500, order: str = "date") -> list[dict[str, Any]]:
        sql = self._LATEST_CTE + f"""
        SELECT p.id, p.message_id, p.posted_at, p.text, p.channel_id,
               c.identifier, c.title AS channel_title, c.chat_id,
               COALESCE(l.views,0) AS views, COALESCE(l.comments,0) AS comments,
               COALESCE(l.reactions,0) AS reactions, COALESCE(l.shares,0) AS shares,
               (SELECT COUNT(*) FROM comments cm
                  WHERE cm.workspace_id=p.workspace_id AND cm.post_id=p.id AND cm.is_deleted=false) AS collected_comments,
               l.taken_at AS updated_at,
               COALESCE(l.views,0) - COALESCE(d.views, f.views, COALESCE(l.views,0)) AS views_delta
          FROM posts p JOIN channels c ON c.id=p.channel_id AND c.workspace_id=p.workspace_id
          LEFT JOIN latest l ON l.workspace_id=p.workspace_id AND l.post_id=p.id
          LEFT JOIN dayago d ON d.post_id=p.id LEFT JOIN firstsnap f ON f.post_id=p.id
         WHERE p.workspace_id=:workspace_id AND (CAST(:channel_id AS bigint) IS NULL OR p.channel_id=CAST(:channel_id AS bigint))
         ORDER BY {self._ORDER_SQL.get(order, 'p.posted_at DESC')} LIMIT :limit"""
        result = await self._execute(sql, {"channel_id": channel_id, "limit": max(1, min(int(limit), 100000))})
        return [dict(row) for row in result.mappings().all()]

    async def kpis(self, channel_id: int | None = None) -> dict[str, Any]:
        sql = self._LATEST_CTE + """
        SELECT COUNT(*) AS posts,
               COALESCE(SUM(l.views),0) AS views, COALESCE(SUM(l.comments),0) AS comments,
               COALESCE(SUM(l.reactions),0) AS reactions, COALESCE(SUM(l.shares),0) AS shares,
               (SELECT COUNT(*) FROM comments cm JOIN posts cp ON cp.id=cm.post_id AND cp.workspace_id=cm.workspace_id
                 WHERE cm.workspace_id=:workspace_id AND cm.is_deleted=false
                   AND (CAST(:channel_id AS bigint) IS NULL OR cp.channel_id=CAST(:channel_id AS bigint))) AS collected_comments,
               (SELECT MAX(s.taken_at) FROM snapshots s JOIN posts pp ON pp.id=s.post_id AND pp.workspace_id=s.workspace_id
                 WHERE s.workspace_id=:workspace_id AND (CAST(:channel_id AS bigint) IS NULL OR pp.channel_id=CAST(:channel_id AS bigint))) AS last_poll
          FROM posts p LEFT JOIN latest l ON l.workspace_id=p.workspace_id AND l.post_id=p.id
         WHERE p.workspace_id=:workspace_id AND (CAST(:channel_id AS bigint) IS NULL OR p.channel_id=CAST(:channel_id AS bigint))"""
        result = await self._execute(sql, {"channel_id": channel_id})
        row = result.mappings().first()
        return dict(row) if row else {}

    async def post_row(self, post_id: int) -> dict[str, Any] | None:
        sql = self._LATEST_CTE + """
        SELECT p.*, c.identifier, c.title AS channel_title, c.chat_id,
               COALESCE(l.views,0) AS views, COALESCE(l.comments,0) AS comments,
               COALESCE(l.reactions,0) AS reactions, COALESCE(l.shares,0) AS shares,
               (SELECT COUNT(*) FROM comments cm WHERE cm.workspace_id=p.workspace_id
                  AND cm.post_id=p.id AND cm.is_deleted=false) AS collected_comments
          FROM posts p JOIN channels c ON c.id=p.channel_id AND c.workspace_id=p.workspace_id
          LEFT JOIN latest l ON l.workspace_id=p.workspace_id AND l.post_id=p.id
         WHERE p.workspace_id=:workspace_id AND p.id=:post_id"""
        result = await self._execute(sql, {"post_id": post_id})
        row = result.mappings().first()
        return dict(row) if row else None

    async def post_history(self, post_id: int) -> list[dict[str, Any]]:
        result = await self._execute(
            """SELECT * FROM snapshots WHERE workspace_id=:workspace_id AND post_id=:post_id
               ORDER BY taken_at ASC, id ASC""",
            {"post_id": post_id},
        )
        return [dict(row) for row in result.mappings().all()]

    async def timeseries_totals(self, days: int | None, channel_id: int | None = None) -> dict[str, list]:
        workspace_id = self._workspace()
        now = datetime.now(timezone.utc)
        async with self.sessions.session() as session:
            if days is None:
                start_row = (
                    await session.execute(
                        text(
                            """SELECT MIN(posted_at) FROM posts
                               WHERE workspace_id=:workspace_id AND (CAST(:channel_id AS bigint) IS NULL OR channel_id=CAST(:channel_id AS bigint))"""
                        ),
                        {"workspace_id": workspace_id, "channel_id": channel_id},
                    )
                ).first()
                start_value = _aware(start_row[0] if start_row else None)
                start_date = (start_value or now).replace(hour=0, minute=0, second=0, microsecond=0)
            else:
                start_date = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=max(1, days) - 1)
            rows = (
                await session.execute(
                    text(
                        """SELECT s.taken_at, s.post_id, s.views, s.comments, s.reactions, s.shares
                           FROM snapshots s JOIN posts p ON p.id=s.post_id AND p.workspace_id=s.workspace_id
                          WHERE s.workspace_id=:workspace_id AND (CAST(:channel_id AS bigint) IS NULL OR p.channel_id=CAST(:channel_id AS bigint))
                          ORDER BY s.taken_at ASC, s.id ASC"""
                    ),
                    {"workspace_id": workspace_id, "channel_id": channel_id},
                )
            ).mappings().all()
            new_rows = (
                await session.execute(
                    text(
                        """SELECT posted_at::date AS day, COUNT(*) AS n FROM posts
                           WHERE workspace_id=:workspace_id AND (CAST(:channel_id AS bigint) IS NULL OR channel_id=CAST(:channel_id AS bigint))
                             AND posted_at >= :since GROUP BY posted_at::date"""
                    ),
                    {"workspace_id": workspace_id, "channel_id": channel_id, "since": start_date},
                )
            ).mappings().all()
        span = max(1, (now.date() - start_date.date()).days + 1)
        days_list = [(start_date + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(span)]
        state: dict[int, tuple[int, int, int, int]] = {}
        by_day: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            taken = _aware(row["taken_at"])
            day = taken.strftime("%Y-%m-%d") if taken else days_list[0]
            data = dict(row)
            if day < days_list[0]:
                state[int(row["post_id"])] = (row["views"], row["comments"], row["reactions"], row["shares"])
            else:
                by_day.setdefault(day, []).append(data)
        posts_per_day = {str(row["day"]): int(row["n"]) for row in new_rows}
        output = {key: [] for key in ("views", "comments", "reactions", "shares", "posts_per_day")}
        for day in days_list:
            for row in by_day.get(day, []):
                state[int(row["post_id"])] = (row["views"], row["comments"], row["reactions"], row["shares"])
            values = list(state.values()) or [(0, 0, 0, 0)]
            output["views"].append(sum(v[0] for v in values))
            output["comments"].append(sum(v[1] for v in values))
            output["reactions"].append(sum(v[2] for v in values))
            output["shares"].append(sum(v[3] for v in values))
            output["posts_per_day"].append(posts_per_day.get(day, 0))
        return {"days": days_list, **output}

    # ---------- import diagnostics and overlap-safe jobs ----------

    async def history_diagnostic(self, channel_id: int | None = None) -> dict[str, Any]:
        result = await self._execute(
            """SELECT COUNT(*) AS total_posts,
                      COUNT(*) FILTER (WHERE char_length(p.text) > 500) AS posts_over_500,
                      COUNT(*) FILTER (WHERE char_length(p.text) = 500) AS posts_at_500,
                      COUNT(*) FILTER (WHERE p.posted_at >= now() - interval '30 days') AS eligible_posts,
                      MIN(p.posted_at) AS oldest_post, MAX(p.posted_at) AS newest_post,
                      COUNT(DISTINCT p.channel_id) AS channels,
                      COALESCE(jsonb_agg(DISTINCT jsonb_build_object('channel_id', p.channel_id))
                               FILTER (WHERE char_length(p.text) = 500), '[]'::jsonb) AS channels_requiring_recollection
                 FROM posts p WHERE p.workspace_id=:workspace_id
                   AND (CAST(:channel_id AS bigint) IS NULL OR p.channel_id=CAST(:channel_id AS bigint))""",
            {"channel_id": channel_id},
        )
        row = result.mappings().first()
        return dict(row) if row else {}

    async def claim_collection_job(self, channel_id: int, lease_seconds: int = 900) -> uuid.UUID | None:
        workspace_id = self._workspace()
        job_id = uuid.uuid4()
        lease_until = datetime.now(timezone.utc) + timedelta(seconds=max(30, lease_seconds))
        try:
            async with self.sessions.session() as session:
                await session.execute(
                    text(
                        """UPDATE collection_jobs SET status='expired', finished_at=now(), error_code='lease_expired'
                           WHERE workspace_id=:workspace_id AND channel_id=:channel_id AND status='running'
                             AND lease_until < now()"""
                    ),
                    {"workspace_id": workspace_id, "channel_id": channel_id},
                )
                await session.execute(
                    text(
                        """INSERT INTO collection_jobs(id, workspace_id, channel_id, status, lease_until)
                           VALUES (:id, :workspace_id, :channel_id, 'running', :lease_until)"""
                    ),
                    {"id": job_id, "workspace_id": workspace_id, "channel_id": channel_id, "lease_until": lease_until},
                )
                await session.commit()
                return job_id
        except IntegrityError:
            return None

    async def finish_collection_job(self, job_id: uuid.UUID, *, status: str = "succeeded", error: str | None = None) -> None:
        await self._execute(
            """UPDATE collection_jobs SET status=:status, finished_at=now(), lease_until=now(),
                      error_code=CASE WHEN CAST(:error AS text) IS NULL THEN NULL ELSE 'collector_error' END,
                      error_detail=:error
               WHERE workspace_id=:workspace_id AND id=:job_id""",
            {"job_id": job_id, "status": status, "error": (error or "")[:1000] if error else None},
        )

    async def expire_stale_collection_jobs(self) -> int:
        """Mark running jobs with expired leases as failed (startup recovery)."""

        result = await self._execute(
            """UPDATE collection_jobs SET status='failed', finished_at=now(),
                      error_code='stale_on_startup', error_detail='stale_on_startup'
               WHERE workspace_id=:workspace_id AND status='running' AND lease_until < now()""",
        )
        return int(result.rowcount or 0)

    async def renew_collection_job(self, job_id: uuid.UUID, lease_seconds: int = 900) -> None:
        """Refresh lease for a long-running collection job."""

        lease_until = datetime.now(timezone.utc) + timedelta(seconds=max(30, lease_seconds))
        await self._execute(
            """UPDATE collection_jobs SET lease_until=:lease_until
               WHERE workspace_id=:workspace_id AND id=:job_id AND status='running'""",
            {"job_id": job_id, "lease_until": lease_until},
        )

    async def post_ids_with_comments(self, channel_id: int) -> list[int]:
        """Return distinct post IDs that have at least one non-deleted comment."""

        result = await self._execute(
            """SELECT DISTINCT post_id FROM comments
               WHERE workspace_id=:workspace_id AND post_id IN (
                   SELECT id FROM posts WHERE workspace_id=:workspace_id AND channel_id=:channel_id
               ) AND is_deleted=false""",
            {"channel_id": channel_id},
        )
        return [int(row[0]) for row in result.all()]
