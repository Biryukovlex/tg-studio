"""Import a legacy stats.db into a migrated PostgreSQL database."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.migration.sqlite_to_postgres import import_sqlite


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", required=True, help="path to the existing stats.db")
    parser.add_argument("--database-url", default=None, help="deprecated: use DATABASE_URL env instead")
    parser.add_argument("--workspace-slug", default="community")
    parser.add_argument("--dry-run", action="store_true", help="inspect source only; never connect/write")
    parser.add_argument("--force", action="store_true", help="merge into a workspace that already has rows")
    args = parser.parse_args()
    if args.database_url:
        print("Do not pass the database URL on argv; set DATABASE_URL in the environment instead.", file=sys.stderr)
        raise SystemExit(2)
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        print("DATABASE_URL is empty; set it in the environment.", file=sys.stderr)
        raise SystemExit(2)
    report = asyncio.run(
        import_sqlite(
            args.sqlite,
            database_url,
            workspace_slug=args.workspace_slug,
            dry_run=args.dry_run,
            force=args.force,
        )
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
