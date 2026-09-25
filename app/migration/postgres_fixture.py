"""Sanitized SQLite-to-PostgreSQL fixture import and reconciliation helpers.

This module is intentionally limited to the four legacy archive tables used by
the M0 proof. It is not the production importer; M1 will add the restartable,
workspace-aware importer against the final SQLAlchemy model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from .sqlite_to_postgres import import_sqlite

IMPORT_TABLES = ("channels", "posts", "snapshots", "comments")


# Keep the M0 test/helper API stable while routing fixture imports through the
# same workspace-aware, type-normalising importer used for the real cutover.
async def import_fixture(
    sqlite_path: str | Path, database_url: str
) -> dict[str, Any]:
    # M0's synthetic fixture must not claim the live/default workspace when
    # the whole PostgreSQL test suite shares a disposable database.
    return await import_sqlite(sqlite_path, database_url, workspace_slug=f"m0-fixture-{uuid4().hex}")
