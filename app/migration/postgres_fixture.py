"""Sanitized SQLite-to-PostgreSQL fixture import and reconciliation helpers.

This module is intentionally limited to the four legacy archive tables used by
the M0 proof. It is not the production importer; M1 will add the restartable,
workspace-aware importer against the final SQLAlchemy model.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, Table, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from .sqlite_inventory import _read_only_connection, _stable_value
from .sqlite_to_postgres import import_sqlite

IMPORT_TABLES = ("channels", "posts", "snapshots", "comments")


def _async_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + database_url.removeprefix("postgresql://")
    return database_url


def _digest(columns: list[str], rows: list[tuple[Any, ...]]) -> str:
    del columns  # Column ordering is already represented by each row's tuple.
    digest = hashlib.sha256()
    for row in rows:
        encoded = json.dumps(
            [_stable_value(value) for value in row],
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        digest.update(encoded)
        digest.update(b"\n")
    return digest.hexdigest()


def _sqlite_rows(
    sqlite_path: str | Path,
) -> dict[str, tuple[list[str], list[tuple[Any, ...]]]]:
    result: dict[str, tuple[list[str], list[tuple[Any, ...]]]] = {}
    with _read_only_connection(sqlite_path) as connection:
        for table in IMPORT_TABLES:
            columns = [
                str(row["name"])
                for row in connection.execute(f'PRAGMA table_info("{table}")')
            ]
            rows = [
                tuple(row[column] for column in columns)
                for row in connection.execute(
                    f'SELECT * FROM "{table}" ORDER BY "id"'
                )
            ]
            result[table] = (columns, rows)
    return result


async def import_fixture(
    sqlite_path: str | Path, database_url: str
) -> dict[str, Any]:
    """Replace the M0 probe tables with fixture rows and return reconciliation."""
    source = _sqlite_rows(sqlite_path)
    engine = create_async_engine(_async_url(database_url), pool_pre_ping=True)
    try:
        async with engine.begin() as connection:
            metadata = MetaData()
            await connection.run_sync(
                lambda sync_connection: metadata.reflect(
                    sync_connection, only=list(IMPORT_TABLES)
                )
            )
            table_map: dict[str, Table] = {
                name: metadata.tables[name] for name in IMPORT_TABLES
            }

            # M0 fixture runs are repeatable against the same disposable DB.
            await connection.execute(
                text(
                    'TRUNCATE TABLE "comments", "snapshots", "posts", "channels" '
                    "RESTART IDENTITY CASCADE"
                )
            )
            for table_name in IMPORT_TABLES:
                columns, rows = source[table_name]
                if rows:
                    await connection.execute(
                        table_map[table_name].insert(),
                        [dict(zip(columns, row)) for row in rows],
                    )

            table_reports: list[dict[str, Any]] = []
            for table_name in IMPORT_TABLES:
                columns, sqlite_rows = source[table_name]
                table = table_map[table_name]
                order_columns = list(table.primary_key.columns) or list(table.columns)
                result = await connection.execute(select(table).order_by(*order_columns))
                postgres_rows = [
                    tuple(row._mapping[column] for column in columns)
                    for row in result
                ]
                sqlite_hash = _digest(columns, sqlite_rows)
                postgres_hash = _digest(columns, postgres_rows)
                table_reports.append(
                    {
                        "name": table_name,
                        "sqlite_count": len(sqlite_rows),
                        "postgres_count": len(postgres_rows),
                        "sqlite_sha256": sqlite_hash,
                        "postgres_sha256": postgres_hash,
                        "match": len(sqlite_rows) == len(postgres_rows)
                        and sqlite_hash == postgres_hash,
                    }
                )
    finally:
        await engine.dispose()

    return {
        "tables": table_reports,
        "all_match": all(report["match"] for report in table_reports),
    }


# Keep the M0 test/helper API stable while routing fixture imports through the
# same workspace-aware, type-normalising importer used for the real cutover.
async def import_fixture(
    sqlite_path: str | Path, database_url: str
) -> dict[str, Any]:
    return await import_sqlite(sqlite_path, database_url)
