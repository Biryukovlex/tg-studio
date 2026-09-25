"""Restartable, read-only SQLite archive importer for the M1 cutover.

The source is opened with SQLite's ``mode=ro`` URI. Rows are imported in
dependency order with destination-owned IDs and the report compares canonical
content, so a restart after an interruption is safe and a mismatch never
gets silently finalized.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
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
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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


def _canonical_records(
    table: str,
    columns: list[str],
    rows: list[dict[str, Any]],
    channel_keys: dict[Any, str],
    post_keys: dict[Any, tuple[str, int]],
) -> Counter[str]:
    """Compare archive content without database-local surrogate IDs."""
    records: Counter[str] = Counter()
    for row in rows:
        values = []
        for column in columns:
            if column == "id":
                continue
            value = row[column]
            if column == "channel_id" and table == "posts":
                value = channel_keys[value]
            elif column == "post_id" and table in {"snapshots", "comments"}:
                value = post_keys[value]
            values.append(_stable_value(_legacy_value(value, column)))
        records[json.dumps(values, ensure_ascii=False, separators=(",", ":"), allow_nan=False)] += 1
    return records


def _records_digest(records: Counter[str]) -> str:
    digest = hashlib.sha256()
    for record, count in sorted(records.items()):
        for _ in range(count):
            digest.update(record.encode("utf-8"))
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
                parsed = [
                    moment
                    for moment in (_timestamp(row[column]) for row in rows if row.get(column))
                    if moment is not None
                ]
                if parsed:
                    item[column] = {
                        "min": min(parsed).isoformat(),
                        "max": max(parsed).isoformat(),
                    }
        if table == "posts":
            item["long_text_posts"] = sum(len(str(row.get("text") or "")) > 500 for row in rows)
            item["channel_counts"] = {
                str(channel_id): sum(row.get("channel_id") == channel_id for row in rows)
                for channel_id in sorted({row.get("channel_id") for row in rows}, key=lambda value: str(value))
            }
        output[table] = item
    return output


def _row_values(table: str, source: dict[str, Any], workspace_id: uuid.UUID) -> dict[str, Any]:
    values = dict(source)
    # A SQLite primary key is local to its archive. PostgreSQL sequences own
    # the destination IDs, including when another workspace was imported first.
    values.pop("id", None)
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


async def _target_records(
    session: Any,
    workspace_id: uuid.UUID,
    source: dict[str, tuple[list[str], list[dict[str, Any]]]],
) -> dict[str, Counter[str]]:
    target: dict[str, list[dict[str, Any]]] = {}
    for table, (columns, _) in source.items():
        selected = ", ".join('"' + column.replace('"', '""') + '"' for column in columns)
        result = await session.execute(
            text(f'SELECT {selected} FROM "{table}" WHERE workspace_id=:workspace_id'),
            {"workspace_id": workspace_id},
        )
        target[table] = [dict(zip(columns, row)) for row in result]
    channel_keys = {row["id"]: row["identifier"] for row in target["channels"]}
    post_keys = {
        row["id"]: (channel_keys[row["channel_id"]], row["message_id"])
        for row in target["posts"]
    }
    return {
        table: _canonical_records(table, columns, target[table], channel_keys, post_keys)
        for table, (columns, _) in source.items()
    }


def _source_records(
    source: dict[str, tuple[list[str], list[dict[str, Any]]]],
) -> dict[str, Counter[str]]:
    channels = {row["id"]: row["identifier"] for row in source["channels"][1]}
    posts = {
        row["id"]: (channels[row["channel_id"]], row["message_id"])
        for row in source["posts"][1]
    }
    return {
        table: _canonical_records(table, columns, rows, channels, posts)
        for table, (columns, rows) in source.items()
    }


def _report(
    source: dict[str, tuple[list[str], list[dict[str, Any]]]],
    source_records: dict[str, Counter[str]],
    target_records: dict[str, Counter[str]],
    *,
    allow_extra: bool = False,
) -> list[dict[str, Any]]:
    reports = []
    for table in IMPORT_TABLES:
        left, right = source_records[table], target_records[table]
        matched = not (left - right) if allow_extra else left == right
        reports.append({
            "name": table,
            "sqlite_count": len(source[table][1]),
            "postgres_count": sum(right.values()),
            "sqlite_sha256": _records_digest(left),
            "postgres_sha256": _records_digest(right),
            "match": matched,
        })
    return reports


async def import_sqlite(
    sqlite_path: str | Path,
    database_url: str,
    *,
    workspace_slug: str = "community",
    dry_run: bool = False,
    force: bool = False,
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
    source_records = _source_records(source)
    try:
        async with manager.session() as session:
            schema = await session.execute(text("SELECT to_regclass('public.workspaces')"))
            if schema.scalar_one_or_none() is None:
                raise RuntimeError("target database is not migrated; run `alembic upgrade head` first")
            # Refuse to mix an archive into a workspace the collector already
            # wrote to, unless --force is given. This check runs before any
            # INSERT so a refused import leaves the target untouched. An
            # identical re-import is idempotent and returns without writing.
            existing_counts: dict[str, int] = {}
            for table in IMPORT_TABLES:
                count_result = await session.execute(
                    text(f'SELECT COUNT(*) FROM "{table}" WHERE workspace_id=:workspace_id'),
                    {"workspace_id": workspace_id},
                )
                existing_counts[table] = int(count_result.scalar_one())
            if any(existing_counts.values()) and not force:
                target_records = await _target_records(session, workspace_id, source)
                if all(source_records[table] == target_records[table] for table in IMPORT_TABLES):
                    return {
                        "tables": _report(source, source_records, target_records),
                        "diagnostics": _diagnostics(source),
                        "all_match": True,
                        "workspace_id": str(workspace_id),
                        "workspace_slug": workspace_slug.strip() or "community",
                        "source_read_only": True,
                        "source_untouched": source_untouched,
                        "restartable": True,
                    }
                raise RuntimeError(
                    "target workspace already contains rows; re-run with force=True to merge "
                    f"(existing={existing_counts})"
                )
            await session.execute(
                text(
                    """INSERT INTO workspaces(id, slug, name) VALUES (:id, :slug, :name)
                       ON CONFLICT (slug) DO NOTHING"""
                ),
                {"id": workspace_id, "slug": workspace_slug.strip() or "community", "name": "Community"},
            )
            await session.flush()

            # Build SQLite-ID -> PostgreSQL-ID maps so foreign keys are remapped
            # instead of mis-attaching posts to live channels.
            channel_id_map: dict[Any, Any] = {}
            post_id_map: dict[Any, Any] = {}
            # Force may merge with existing rows, but never overwrites a row
            # merely because its unrelated SQLite archive reused its ID.
            existing_snapshot_counts: Counter[tuple[Any, ...]] = Counter()
            if force:
                snapshot_result = await session.execute(
                    text("SELECT post_id, taken_at, views, comments, reactions, shares "
                         "FROM snapshots WHERE workspace_id=:workspace_id"),
                    {"workspace_id": workspace_id},
                )
                existing_snapshot_counts.update(tuple(row) for row in snapshot_result)
            seen_snapshot_counts: Counter[tuple[Any, ...]] = Counter()
            try:
                for table in IMPORT_TABLES:
                    columns, rows = source[table]
                    if table == "channels":
                        sql = text(
                            """INSERT INTO channels(workspace_id, identifier, title, chat_id, active, created_at)
                               VALUES (:workspace_id, :identifier, :title, :chat_id, :active, :created_at)
                               ON CONFLICT (workspace_id, identifier) DO UPDATE SET
                                   title=EXCLUDED.title, chat_id=EXCLUDED.chat_id, active=EXCLUDED.active"""
                        )
                    elif table == "posts":
                        sql = text(
                            """INSERT INTO posts(
                                   workspace_id, channel_id, message_id, posted_at, text,
                                   formatting_entities, is_deleted, created_at
                               )
                               VALUES (
                                   :workspace_id, :channel_id, :message_id, :posted_at, :text,
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
                            """INSERT INTO snapshots(workspace_id, post_id, taken_at, views, comments, reactions, shares)
                               VALUES (:workspace_id, :post_id, :taken_at, :views, :comments, :reactions, :shares)"""
                        )
                    else:
                        sql = text(
                            """INSERT INTO comments(
                                   workspace_id, post_id, telegram_message_id, discussion_chat_id,
                                   discussion_username, sender_id, sender_name, sender_username,
                                   posted_at, edited_at, text, media_type, reactions,
                                   reply_to_message_id, is_deleted, first_collected_at,
                                   last_collected_at, last_seen_sync
                               ) VALUES (
                                   :workspace_id, :post_id, :telegram_message_id, :discussion_chat_id,
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
                        values = _row_values(table, source_row, workspace_id)
                        if table == "posts":
                            sqlite_channel = source_row.get("channel_id")
                            values["channel_id"] = channel_id_map.get(sqlite_channel, sqlite_channel)
                        elif table in ("snapshots", "comments"):
                            sqlite_post = source_row.get("post_id")
                            values["post_id"] = post_id_map.get(sqlite_post, sqlite_post)
                        if table == "snapshots" and force:
                            key = tuple(values[field] for field in (
                                "post_id", "taken_at", "views", "comments", "reactions", "shares"
                            ))
                            seen_snapshot_counts[key] += 1
                            if seen_snapshot_counts[key] <= existing_snapshot_counts[key]:
                                continue
                        await session.execute(sql, values)
                        await session.flush()
                        if table == "channels":
                            resolved = await session.execute(
                                text("SELECT id FROM channels WHERE workspace_id=:workspace_id AND identifier=:identifier"),
                                {"workspace_id": workspace_id, "identifier": values["identifier"]},
                            )
                            channel_id_map[source_row.get("id")] = resolved.scalar_one()
                        elif table == "posts":
                            resolved_post = await session.execute(
                                text(
                                    "SELECT id FROM posts WHERE workspace_id=:workspace_id "
                                    "AND channel_id=:channel_id AND message_id=:message_id"
                                ),
                                {"workspace_id": workspace_id, "channel_id": values["channel_id"],
                                 "message_id": values["message_id"]},
                            )
                            post_id_map[source_row.get("id")] = resolved_post.scalar_one()
                    await session.flush()

                target_records = await _target_records(session, workspace_id, source)
                reports = _report(source, source_records, target_records, allow_extra=force)
                all_match = all(report["match"] for report in reports)
                if not all_match:
                    await session.rollback()
                    raise RuntimeError(json.dumps({"tables": reports, "all_match": False}, ensure_ascii=False))
                await session.commit()
            except Exception:
                try:
                    await session.rollback()
                except Exception:
                    pass
                raise
    finally:
        await manager.dispose()

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
