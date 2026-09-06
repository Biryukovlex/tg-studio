"""Import a legacy stats.db into a migrated PostgreSQL database."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.migration.sqlite_to_postgres import import_sqlite


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", required=True, help="path to the existing stats.db")
    parser.add_argument("--database-url", required=True, help="PostgreSQL connection URL")
    parser.add_argument("--workspace-slug", default="community")
    parser.add_argument("--dry-run", action="store_true", help="inspect source only; never connect/write")
    args = parser.parse_args()
    report = asyncio.run(
        import_sqlite(
            args.sqlite,
            args.database_url,
            workspace_slug=args.workspace_slug,
            dry_run=args.dry_run,
        )
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
