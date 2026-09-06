"""Build a deterministic, content-safe inventory of a SQLite archive.

The inventory intentionally reports only schema and aggregate metadata. Row
values are used to calculate one-way hashes, but message/comment text is never
written to the report. The source database is opened with SQLite's read-only
URI mode and no write-capable pragmas are executed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable


_TIMESTAMP_SUFFIXES = ("_at", "_date", "_time")


def _identifier(value: str) -> str:
    """Quote a SQLite identifier without allowing SQL syntax into a query."""
    return '"' + value.replace('"', '""') + '"'


def _read_only_connection(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"SQLite archive does not exist: {path}")
    # `mode=ro` prevents accidental creation or mutation of the archive. Do
    # not use `immutable=1`: a live WAL sidecar must remain visible to reads.
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _tables(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        """SELECT name FROM sqlite_master
           WHERE type='table' AND name NOT LIKE 'sqlite_%'
           ORDER BY name"""
    ).fetchall()
    return [str(row["name"]) for row in rows]


def _columns(connection: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    return connection.execute(
        f"PRAGMA table_info({_identifier(table)})"
    ).fetchall()


def _stable_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return {"__bytes_sha256__": hashlib.sha256(value).hexdigest()}
    return {"__repr__": repr(value)}


def _row_hash(
    connection: sqlite3.Connection,
    table: str,
    column_rows: Iterable[sqlite3.Row],
) -> str:
    columns = [str(row["name"]) for row in column_rows]
    primary_key = [str(row["name"]) for row in column_rows if row["pk"]]
    order_by = primary_key or columns
    order_sql = ", ".join(_identifier(column) for column in order_by)
    query = f"SELECT * FROM {_identifier(table)}"
    if order_sql:
        query += f" ORDER BY {order_sql}"

    digest = hashlib.sha256()
    for row in connection.execute(query):
        values = [_stable_value(row[column]) for column in columns]
        encoded = json.dumps(
            values, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        digest.update(encoded)
        digest.update(b"\n")
    return digest.hexdigest()


def _timestamp_ranges(
    connection: sqlite3.Connection,
    table: str,
    column_rows: Iterable[sqlite3.Row],
) -> dict[str, dict[str, str | None]]:
    ranges: dict[str, dict[str, str | None]] = {}
    for row in column_rows:
        name = str(row["name"])
        lower_name = name.lower()
        if not lower_name.endswith(_TIMESTAMP_SUFFIXES):
            continue
        result = connection.execute(
            f"SELECT MIN({_identifier(name)}) AS minimum, "
            f"MAX({_identifier(name)}) AS maximum "
            f"FROM {_identifier(table)}"
        ).fetchone()
        ranges[name] = {
            "min": result["minimum"],
            "max": result["maximum"],
        }
    return ranges


def _nullability_violations(
    connection: sqlite3.Connection,
    table: str,
    column_rows: Iterable[sqlite3.Row],
) -> dict[str, int]:
    violations: dict[str, int] = {}
    for row in column_rows:
        if not row["notnull"]:
            continue
        name = str(row["name"])
        result = connection.execute(
            f"SELECT COUNT(*) AS count FROM {_identifier(table)} "
            f"WHERE {_identifier(name)} IS NULL"
        ).fetchone()
        count = int(result["count"])
        if count:
            violations[name] = count
    return violations


def _unique_indexes(
    connection: sqlite3.Connection, table: str
) -> list[dict[str, Any]]:
    indexes = connection.execute(
        f"PRAGMA index_list({_identifier(table)})"
    ).fetchall()
    result: list[dict[str, Any]] = []
    for index in indexes:
        if not index["unique"]:
            continue
        name = str(index["name"])
        index_columns = connection.execute(
            f"PRAGMA index_info({_identifier(name)})"
        ).fetchall()
        columns = [row["name"] for row in index_columns]
        item: dict[str, Any] = {
            "name": name,
            "columns": columns,
            "partial": bool(index["partial"]),
            "duplicate_groups": 0,
            "duplicate_rows": 0,
        }
        # Expression indexes have no column name and cannot be inspected with
        # this generic GROUP BY query. Keep the limitation explicit.
        if not columns or any(column is None for column in columns):
            item["inspection"] = "unsupported_expression_index"
            result.append(item)
            continue
        if index["partial"]:
            item["inspection"] = "partial_index_not_evaluated"
            result.append(item)
            continue

        quoted_columns = ", ".join(_identifier(str(column)) for column in columns)
        non_null = " AND ".join(
            f"{_identifier(str(column))} IS NOT NULL" for column in columns
        )
        duplicate_query = f"""
            SELECT COUNT(*) AS duplicate_groups,
                   COALESCE(SUM(group_count - 1), 0) AS duplicate_rows
              FROM (
                    SELECT {quoted_columns}, COUNT(*) AS group_count
                      FROM {_identifier(table)}
                     WHERE {non_null}
                     GROUP BY {quoted_columns}
                    HAVING COUNT(*) > 1
                   )
        """
        duplicate = connection.execute(duplicate_query).fetchone()
        item["duplicate_groups"] = int(duplicate["duplicate_groups"])
        item["duplicate_rows"] = int(duplicate["duplicate_rows"])
        result.append(item)
    return sorted(result, key=lambda item: item["name"])


def _channel_inventory(connection: sqlite3.Connection) -> list[dict[str, int]]:
    tables = set(_tables(connection))
    if "channels" not in tables or "posts" not in tables:
        return []

    ids = {
        int(row["id"])
        for row in connection.execute("SELECT id FROM channels ORDER BY id")
    }
    ids.update(
        int(row["channel_id"])
        for row in connection.execute(
            "SELECT DISTINCT channel_id FROM posts WHERE channel_id IS NOT NULL"
        )
    )

    inventory: list[dict[str, int]] = []
    for channel_id in sorted(ids):
        post_count = connection.execute(
            "SELECT COUNT(*) AS count FROM posts WHERE channel_id=?", (channel_id,)
        ).fetchone()["count"]
        snapshots = 0
        comments = 0
        if "snapshots" in tables:
            snapshots = connection.execute(
                """SELECT COUNT(*) AS count FROM snapshots s
                   JOIN posts p ON p.id=s.post_id
                  WHERE p.channel_id=?""",
                (channel_id,),
            ).fetchone()["count"]
        if "comments" in tables:
            comments = connection.execute(
                """SELECT COUNT(*) AS count FROM comments c
                   JOIN posts p ON p.id=c.post_id
                  WHERE p.channel_id=?""",
                (channel_id,),
            ).fetchone()["count"]
        inventory.append(
            {
                "channel_db_id": channel_id,
                "post_count": int(post_count),
                "snapshot_count": int(snapshots),
                "comment_count": int(comments),
            }
        )
    return inventory


def _foreign_key_violation_count(connection: sqlite3.Connection) -> int:
    return sum(1 for _ in connection.execute("PRAGMA foreign_key_check"))


def build_inventory(db_path: str | Path) -> dict[str, Any]:
    """Return a deterministic archive inventory without exposing row values."""
    path = Path(db_path).expanduser().resolve()
    with _read_only_connection(path) as connection:
        table_reports: list[dict[str, Any]] = []
        for table in _tables(connection):
            column_rows = _columns(connection, table)
            schema_row = connection.execute(
                """SELECT sql FROM sqlite_master
                   WHERE type='table' AND name=?""",
                (table,),
            ).fetchone()
            count = connection.execute(
                f"SELECT COUNT(*) AS count FROM {_identifier(table)}"
            ).fetchone()["count"]
            table_reports.append(
                {
                    "name": table,
                    "schema_sql": schema_row["sql"] if schema_row else None,
                    "row_count": int(count),
                    "columns": [
                        {
                            "name": str(row["name"]),
                            "type": str(row["type"] or ""),
                            "not_null": bool(row["notnull"]),
                            "default": row["dflt_value"],
                            "primary_key_position": int(row["pk"]),
                        }
                        for row in column_rows
                    ],
                    "timestamp_ranges": _timestamp_ranges(
                        connection, table, column_rows
                    ),
                    "nullability_violations": _nullability_violations(
                        connection, table, column_rows
                    ),
                    "unique_indexes": _unique_indexes(connection, table),
                    "row_sha256": _row_hash(connection, table, column_rows),
                }
            )

        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        integrity_ok = len(integrity) == 1 and integrity[0][0] == "ok"
        return {
            "report_version": 1,
            "source_filename": path.name,
            "tables": table_reports,
            "per_channel": _channel_inventory(connection),
            "integrity": {
                "sqlite_integrity_check_ok": integrity_ok,
                "foreign_key_violation_count": _foreign_key_violation_count(
                    connection
                ),
            },
        }


def _json_report(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Create a read-only SQLite archive inventory. The JSON contains "
            "schema, counts, ranges, and hashes; row text is never exported."
        )
    )
    parser.add_argument("--db", required=True, help="Path to the SQLite archive")
    parser.add_argument(
        "--output",
        default="-",
        help="JSON destination; use - for stdout (default)",
    )
    args = parser.parse_args(argv)

    report = build_inventory(args.db)
    payload = _json_report(report)
    if args.output == "-":
        print(payload, end="")
    else:
        output = Path(args.output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through `python -m`
    raise SystemExit(main())
