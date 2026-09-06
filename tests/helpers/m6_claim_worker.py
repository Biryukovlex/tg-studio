"""One-shot independent process used by the M6 PostgreSQL claim proof."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid

from app.postgres_db import PostgresDatabase
from app.studio.repository import StudioRepository


async def _main() -> int:
    if len(sys.argv) != 4 or sys.argv[1] not in {"run", "collection"}:
        return 2
    database_url = os.environ.get("M6_POSTGRES_URL", "")
    workspace_slug = os.environ.get("M6_WORKSPACE_SLUG", "")
    if not database_url or not workspace_slug:
        return 2
    kind, resource, worker_id = sys.argv[1:]
    db = PostgresDatabase(database_url, workspace_slug=workspace_slug, pool_size=1, max_overflow=0)
    # The parent process seeds this deterministic workspace before spawning the
    # claimants. Each child therefore has an independent engine/pool without
    # racing the seed operation itself.
    db.workspace_id = uuid.uuid5(uuid.NAMESPACE_URL, f"telegram-stats:workspace:{workspace_slug}")
    try:
        if kind == "run":
            claimed = await StudioRepository(db).claim_run(
                uuid.UUID(resource), worker_id=worker_id, lease_seconds=120
            )
            result = {"claimed": claimed is not None, "worker_id": worker_id}
        else:
            claimed_id = await db.claim_collection_job(int(resource), lease_seconds=120)
            result = {
                "claimed": claimed_id is not None,
                "worker_id": worker_id,
                "job_id": str(claimed_id) if claimed_id is not None else None,
            }
        print(json.dumps(result, sort_keys=True))
        return 0
    finally:
        await db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
