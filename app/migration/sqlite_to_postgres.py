"""Restartable, read-only SQLite archive importer for the M1 cutover.

The source is opened with SQLite's ``mode=ro`` URI.  Rows are upserted in
dependency order and the report compares counts and canonical hashes, so a
restart after an interruption is safe and a mismatch never gets silently
finalized.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from ..db_session import DatabaseSessionManager, normalize_database_url
from ..telegram_formatting import normalize_entities
from .sqlite_inventory import _read_only_connection, _stable_value

IMPORT_TABLES = ("channels", "posts", "snapshots", "comments")
COMMUNITY_WORKSPACE_ID = uuid.uuid5(uuid.NAMESPACE_URL, "telegram-stats:workspace:community")


def _timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=timezone.utc)


def _legacy_value(value: Any, column: str) -> Any:
    if column == "formatting_entities":
        # SQLite stores the JSON as text while PostgreSQL returns JSONB as a
        # Python list.  Canonical JSON makes reconciliation representation-
        # independent and avoids including provider-specific object details.
        return json.dumps(
            normalize_entities(value),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    if column.endswith("_at") or column.endswith("_date") or column.endswith("_time"):
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if column in {"active", "is_deleted"} and isinstance(value, bool):
        return int(value)
    return value


def _digest(columns: list[str], rows: list[tuple[Any, ...]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        encoded = json.dumps(
            [_stable_value(_legacy_value(value, column)) for column, value in zip(columns, row)],
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        digest.update(encoded)
        digest.update(b"\n")
    return digest.hexdigest()


def _source_rows(sqlite_path: str | Path) -> dict[str, tuple[list[str], list[dict[str, Any]]]]:
    output: dict[str, tuple[list[str], list[dict[str, Any]]]] = {}
    with _read_only_connection(sqlite_path) as connection:
        for table in IMPORT_TABLES:
            columns = [str(row["name"]) for row in connection.execute(f'PRAGMA table_info("{table}")')]
            rows = [dict(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY id')]
            output[table] = (columns, rows)
    return output


def _diagnostics(source: dict[str, tuple[list[str], list[dict[str, Any]]]]) -> dict[str, Any]:
    """Summarise ranges/counts without including any message body content."""
    output: dict[str, Any] = {}
    for table, (columns, rows) in source.items():
        item: dict[str, Any] = {"count": len(rows)}
        for column in columns:
            if column.endswith("_at") or column.endswith("_date") or column.endswith("_time"):
                parsed = [_timestamp(row[column]) for row in rows if row.get(column)]
                if parsed:
                    item[column] = {
                        "min": min(parsed).isoformat(),
                        "max": max(parsed).isoformat(),
                    }
        if table == "posts":
            item["long_text_posts"] = sum(len(str(row.get("text") or "")) > 500 for row in rows)
            item["channel_counts"] = {
                str(channel_id): sum(row.get("channel_id") == channel_id for row in rows)
                for channel_id in sorted({row.get("channel_id") for row in rows})
            }
        output[table] = item
    return output


def _row_values(table: str, source: dict[str, Any], workspace_id: uuid.UUID) -> dict[str, Any]:
    values = dict(source)
    values["workspace_id"] = workspace_id
    if table == "channels":
        values["active"] = bool(values.get("active", 1))
        values.setdefault("created_at", datetime.now(timezone.utc))
    elif table == "posts":
        values["posted_at"] = _timestamp(values.get("posted_at"))
        values["formatting_entities"] = json.dumps(
            normalize_entities(values.get("formatting_entities")),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        values["is_deleted"] = bool(values.get("is_deleted", 0))
        values.setdefault("created_at", datetime.now(timezone.utc))
    elif table == "snapshots":
        values["taken_at"] = _timestamp(values.get("taken_at"))
    elif table == "comments":
        for column in ("posted_at", "edited_at", "first_collected_at", "last_collected_at"):
            values[column] = _timestamp(values.get(column))
        values["is_deleted"] = bool(values.get("is_deleted", 0))
    return values


async def import_sqlite(
    sqlite_path: str | Path,
    database_url: str,
    *,
    workspace_slug: str = "community",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Import the archive and return a safe reconciliation report."""
    source_file = Path(sqlite_path).expanduser().resolve()
    before = source_file.stat()
    source = _source_rows(source_file)
    after = source_file.stat()
    source_untouched = (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
    if not source_untouched:
        raise RuntimeError("SQLite source changed while it was being read; no target finalization is allowed")
    workspace_id = uuid.uuid5(uuid.NAMESPACE_URL, f"telegram-stats:workspace:{workspace_slug.strip() or 'community'}")
    if dry_run:
        return {
            "dry_run": True,
            "source": {name: {"count": len(rows)} for name, (_, rows) in source.items()},
            "diagnostics": _diagnostics(source),
            "all_match": False,
            "message": "dry-run did not write or compare target rows",
            "source_untouched": True,
        }

    manager = DatabaseSessionManager(normalize_database_url(database_url), pool_size=2, max_overflow=1)
    reports: list[dict[str, Any]] = []
    try:
        async with manager.session() as session:
            schema = await session.execute(text("SELECT to_regclass('public.workspaces')"))
            if schema.scalar_one_or_none() is None:
                raise RuntimeError("target database is not migrated; run `alembic upgrade head` first")
            await session.execute(
                text(
                    """INSERT INTO workspaces(id, slug, name) VALUES (:id, :slug, :name)
                       ON CONFLICT (slug) DO NOTHING"""
                ),
                {"id": workspace_id, "slug": workspace_slug.strip() or "community", "name": "Community"},
            )
            await session.commit()

            # Explicit IDs preserve links and make a restart idempotent.
            for table in IMPORT_TABLES:
                columns, rows = source[table]
                if table == "channels":
                    sql = text(
                        """INSERT INTO channels(id, workspace_id, identifier, title, chat_id, active, created_at)
                           VALUES (:id, :workspace_id, :identifier, :title, :chat_id, :active, :created_at)
                           ON CONFLICT (workspace_id, identifier) DO UPDATE SET
                               title=EXCLUDED.title, chat_id=EXCLUDED.chat_id, active=EXCLUDED.active"""
                    )
                elif table == "posts":
                    sql = text(
                        """INSERT INTO posts(
                               id, workspace_id, channel_id, message_id, posted_at, text,
                               formatting_entities, is_deleted, created_at
                           )
                           VALUES (
                               :id, :workspace_id, :channel_id, :message_id, :posted_at, :text,
                               CAST(:formatting_entities AS jsonb), :is_deleted, :created_at
                           )
                           ON CONFLICT (workspace_id, channel_id, message_id) DO UPDATE SET
                               posted_at=EXCLUDED.posted_at,
                               text=EXCLUDED.text,
                               formatting_entities=EXCLUDED.formatting_entities,
                               is_deleted=EXCLUDED.is_deleted"""
                    )
                elif table == "snapshots":
                    sql = text(
                        """INSERT INTO snapshots(id, workspace_id, post_id, taken_at, views, comments, reactions, shares)
                           VALUES (:id, :workspace_id, :post_id, :taken_at, :views, :comments, :reactions, :shares)
                           ON CONFLICT (id) DO UPDATE SET
                               taken_at=EXCLUDED.taken_at, views=EXCLUDED.views, comments=EXCLUDED.comments,
                               reactions=EXCLUDED.reactions, shares=EXCLUDED.shares"""
                    )
                else:
                    sql = text(
                        """INSERT INTO comments(
                               id, workspace_id, post_id, telegram_message_id, discussion_chat_id,
                               discussion_username, sender_id, sender_name, sender_username,
                               posted_at, edited_at, text, media_type, reactions,
                               reply_to_message_id, is_deleted, first_collected_at,
                               last_collected_at, last_seen_sync
                           ) VALUES (
                               :id, :workspace_id, :post_id, :telegram_message_id, :discussion_chat_id,
                               :discussion_username, :sender_id, :sender_name, :sender_username,
                               :posted_at, :edited_at, :text, :media_type, :reactions,
                               :reply_to_message_id, :is_deleted, :first_collected_at,
                               :last_collected_at, :last_seen_sync)
                           ON CONFLICT (workspace_id, post_id, telegram_message_id) DO UPDATE SET
                               discussion_chat_id=EXCLUDED.discussion_chat_id,
                               discussion_username=EXCLUDED.discussion_username,
                               sender_id=EXCLUDED.sender_id, sender_name=EXCLUDED.sender_name,
                               sender_username=EXCLUDED.sender_username, posted_at=EXCLUDED.posted_at,
                               edited_at=EXCLUDED.edited_at, text=EXCLUDED.text,
                               media_type=EXCLUDED.media_type, reactions=EXCLUDED.reactions,
                               reply_to_message_id=EXCLUDED.reply_to_message_id,
                               is_deleted=EXCLUDED.is_deleted, last_collected_at=EXCLUDED.last_collected_at,
                               last_seen_sync=EXCLUDED.last_seen_sync"""
                    )
                for source_row in rows:
                    await session.execute(sql, _row_values(table, source_row, workspace_id))
                await session.commit()

                target_columns = columns
                target_rows_result = await session.execute(
                    text(
                        f'SELECT {", ".join(chr(34) + c + chr(34) for c in target_columns)} '
                        f'FROM "{table}" WHERE workspace_id=:workspace_id ORDER BY id'
                    ),
                    {"workspace_id": workspace_id},
                )
                target_rows = [tuple(row) for row in target_rows_result]
                source_tuples = [tuple(row[column] for column in columns) for row in rows]
                source_hash = _digest(columns, source_tuples)
                target_hash = _digest(columns, target_rows)
                reports.append(
                    {
                        "name": table,
                        "sqlite_count": len(source_tuples),
                        "postgres_count": len(target_rows),
                        "sqlite_sha256": source_hash,
                        "postgres_sha256": target_hash,
                        "match": len(source_tuples) == len(target_rows) and source_hash == target_hash,
                    }
                )

            for table in ("channels", "posts", "snapshots", "comments"):
                await session.execute(
                    text(
                        f"SELECT setval(pg_get_serial_sequence(:table_name, 'id'), "
                        f"COALESCE((SELECT MAX(id) FROM {table}), 1), true)"
                    ),
                    {"table_name": table},
                )
            await session.commit()
    finally:
        await manager.dispose()

    all_match = all(report["match"] for report in reports)
    if not all_match:
        raise RuntimeError(json.dumps({"tables": reports, "all_match": False}, ensure_ascii=False))
    return {
        "tables": reports,
        "diagnostics": _diagnostics(source),
        "all_match": True,
        "workspace_id": str(workspace_id),
        "workspace_slug": workspace_slug.strip() or "community",
        "source_read_only": True,
        "source_untouched": source_untouched,
        "restartable": True,
    }
