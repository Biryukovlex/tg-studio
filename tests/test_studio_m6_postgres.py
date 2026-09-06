import asyncio
import json
import os
from pathlib import Path
import sys
import uuid

import pytest

from app.config import Settings
from app.postgres_db import PostgresDatabase
from app.studio.repository import StudioRepository
from app.studio.service import StudioService


ROOT = Path(__file__).resolve().parents[1]


def _database_url() -> str | None:
    return os.environ.get("M6_POSTGRES_URL")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_expired_lease_recovers_with_durable_event():
    database_url = _database_url()
    if not database_url:
        pytest.skip("set M6_POSTGRES_URL to run the M6 durability proof")
    workspace_slug = f"m6-recovery-{uuid.uuid4().hex[:10]}"
    db = PostgresDatabase(database_url, workspace_slug=workspace_slug)
    try:
        await db.init_db(admin_username=f"{workspace_slug}-admin")
        channel_id = await db.upsert_channel(f"@{workspace_slug}", "M6 recovery", 6101)
        repository = StudioRepository(db)
        conversation = await repository.create_conversation(channel_id=channel_id)
        message = await repository.append_message(
            conversation_id=conversation["id"], role="user", content="recover this run"
        )
        run = await repository.create_run(
            conversation_id=conversation["id"],
            user_message_id=message["id"],
            requested_model="test",
        )
        claimed = await repository.claim_run(run["id"], worker_id="worker-before-restart")
        assert claimed and claimed["status"] == "running"
        await db._execute(
            "UPDATE studio_agent_runs SET lease_expires_at=now() - interval '1 second' "
            "WHERE workspace_id=:workspace_id AND id=:run_id",
            {"run_id": run["id"]},
        )

        restarted = StudioService(
            StudioRepository(db),
            Settings(studio_enabled=True, studio_test_mode=True),
        )
        assert await restarted.recover_stale_runs() == 1
        recovered = await restarted.repository.get_run(run["id"])
        events = await restarted.repository.get_events(run["id"])
        assert recovered and recovered["status"] == "interrupted"
        assert recovered["lease_expires_at"] is None
        assert events[-1]["event_type"] == "RUN_INTERRUPTED"
        assert events[-1]["safe_payload"] == {}
    finally:
        await db.close()


async def _claim_process(*, kind: str, resource: str, worker_id: str, workspace_slug: str):
    env = os.environ.copy()
    env["M6_WORKSPACE_SLUG"] = workspace_slug
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(ROOT), env.get("PYTHONPATH"))))
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(ROOT / "tests/helpers/m6_claim_worker.py"),
        kind,
        resource,
        worker_id,
        cwd=str(ROOT),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=15)
    assert process.returncode == 0, stderr.decode(errors="replace")
    return json.loads(stdout.decode().strip().splitlines()[-1])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_worker_processes_cannot_double_claim_runs_or_collection_jobs():
    database_url = _database_url()
    if not database_url:
        pytest.skip("set M6_POSTGRES_URL to run the M6 multi-worker proof")
    workspace_slug = f"m6-workers-{uuid.uuid4().hex[:10]}"
    db = PostgresDatabase(database_url, workspace_slug=workspace_slug)
    try:
        await db.init_db(admin_username=f"{workspace_slug}-admin")
        channel_id = await db.upsert_channel(f"@{workspace_slug}", "M6 workers", 6102)
        repository = StudioRepository(db)
        conversation = await repository.create_conversation(channel_id=channel_id)
        message = await repository.append_message(
            conversation_id=conversation["id"], role="user", content="claim exactly once"
        )
        run = await repository.create_run(
            conversation_id=conversation["id"],
            user_message_id=message["id"],
            requested_model="test",
        )

        run_results = await asyncio.gather(
            _claim_process(kind="run", resource=str(run["id"]), worker_id="run-worker-a", workspace_slug=workspace_slug),
            _claim_process(kind="run", resource=str(run["id"]), worker_id="run-worker-b", workspace_slug=workspace_slug),
        )
        assert sum(bool(item["claimed"]) for item in run_results) == 1
        winner = next(item["worker_id"] for item in run_results if item["claimed"])
        claimed_run = await repository.get_run(run["id"])
        assert claimed_run and claimed_run["status"] == "running"
        assert claimed_run["worker_id"] == winner

        collection_results = await asyncio.gather(
            _claim_process(kind="collection", resource=str(channel_id), worker_id="collector-a", workspace_slug=workspace_slug),
            _claim_process(kind="collection", resource=str(channel_id), worker_id="collector-b", workspace_slug=workspace_slug),
        )
        assert sum(bool(item["claimed"]) for item in collection_results) == 1
        active = await db._execute(
            """SELECT count(*) FROM collection_jobs
                 WHERE workspace_id=:workspace_id AND channel_id=:channel_id AND status='running'""",
            {"channel_id": channel_id},
        )
        assert active.scalar_one() == 1

        await repository.set_run_status(run["id"], status="succeeded", worker_id=winner)
        job_id = next(item["job_id"] for item in collection_results if item["claimed"])
        await db.finish_collection_job(uuid.UUID(job_id))
    finally:
        await db.close()
