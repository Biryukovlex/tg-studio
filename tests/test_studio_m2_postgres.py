import json
import os
import uuid

import pytest

from app.config import Settings
from app.postgres_db import PostgresDatabase
from app.studio.repository import StudioRepository
from app.studio.service import StudioService


def _payload(conversation_id: uuid.UUID, run_id: uuid.UUID) -> bytes:
    return json.dumps(
        {
            "threadId": str(conversation_id),
            "runId": str(run_id),
            "messages": [{"id": str(uuid.uuid4()), "role": "user", "content": "Inspect the channel context."}],
            "tools": [],
            "context": [],
            "forwardedProps": {},
        }
    ).encode()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_studio_run_persists_workspace_scoped_history_and_events():
    database_url = os.environ.get("M2_POSTGRES_URL")
    if not database_url:
        pytest.skip("set M2_POSTGRES_URL to run the Studio PostgreSQL proof")
    db = PostgresDatabase(database_url)
    try:
        await db.init_db(admin_username="m2-studio-admin")
        channel_id = await db.upsert_channel("@m2-studio", "M2 Studio", 2026)
        repository = StudioRepository(db)
        service = StudioService(repository, Settings(studio_test_mode=True))
        conversation = await repository.create_conversation(channel_id=channel_id)
        run_id = uuid.uuid4()
        response = await service.stream_request(None, _payload(conversation["id"], run_id))
        chunks: list[str] = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        assert response.status_code == 200
        assert '"type":"RUN_FINISHED"' in "".join(chunks)
        messages = await repository.list_messages(conversation["id"])
        assert [message["role"] for message in messages] == ["user", "assistant"]
        run = await repository.get_run(run_id)
        assert run and run["status"] == "succeeded"
        events = await repository.get_events(run_id)
        assert events and events[-1]["event_type"] == "RUN_FINISHED"
        assert all("content" not in event["safe_payload"] for event in events)

        # Recreate the repository/service boundary to prove PostgreSQL, rather
        # than the in-process registry, is authoritative after a reload.
        reloaded_repository = StudioRepository(db)
        reloaded_service = StudioService(
            reloaded_repository,
            Settings(studio_test_mode=True),
        )
        reloaded_messages = await reloaded_repository.list_messages(conversation["id"])
        reloaded_events = await reloaded_repository.get_events(run_id)
        assert [message["role"] for message in reloaded_messages] == ["user", "assistant"]
        assert reloaded_events[-1]["event_type"] == "RUN_FINISHED"
        assert reloaded_service.repository.workspace_id == repository.workspace_id
    finally:
        await db.close()
