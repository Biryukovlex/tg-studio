from __future__ import annotations

import os
import re
from pathlib import Path
import uuid

import pytest

from app.studio.repository import ActiveRunExists, MemoryStudioRepository, RunNotFound, StudioRepository


ROOT = Path(__file__).resolve().parents[1]


async def _login(client, settings) -> str:
    response = await client.post(
        "/login",
        data={"username": settings.admin_username, "password": settings.admin_password},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = await client.get("/studio")
    assert page.status_code == 200
    match = re.search(r'<meta name="studio-csrf-token" content="([^"]+)"', page.text)
    assert match
    return match.group(1)


@pytest.mark.asyncio
async def test_memory_delete_is_scoped_cascades_studio_records_and_blocks_active_runs():
    repository = MemoryStudioRepository(workspace_id="delete-a")
    other = MemoryStudioRepository(workspace_id="delete-b")
    conversation = await repository.create_conversation(channel_id=1, title="Delete me")
    message = await repository.append_message(
        conversation_id=conversation["id"], role="user", content="Keep this scoped"
    )
    run = await repository.create_run(
        conversation_id=conversation["id"], user_message_id=message["id"], requested_model="test"
    )
    draft = await repository.create_draft(
        conversation_id=conversation["id"],
        channel_id=1,
        payload={"body": "Studio-only draft", "creative": True},
    )
    await repository.append_event(run["id"], event_type="RUN_STARTED")

    with pytest.raises(ActiveRunExists):
        await repository.delete_conversation(conversation["id"])
    assert await repository.get_conversation(conversation["id"]) is not None

    await repository.set_run_status(run["id"], status="succeeded")
    assert await repository.delete_conversation(conversation["id"]) is True
    assert await repository.get_conversation(conversation["id"]) is None
    assert await repository.list_conversations() == []
    assert await repository.get_draft(draft["id"]) is None
    with pytest.raises(RunNotFound):
        await repository.get_events(run["id"])
    assert await repository.delete_conversation(conversation["id"]) is False

    foreign = await other.create_conversation(channel_id=1, title="Foreign")
    assert await repository.delete_conversation(foreign["id"]) is False
    assert await other.get_conversation(foreign["id"]) is not None


@pytest.mark.asyncio
async def test_delete_route_requires_csrf_blocks_active_runs_and_removes_history(client, app, settings):
    settings.studio_test_mode = True
    token = await _login(client, settings)

    target_response = await client.post(
        "/studio/api/conversations",
        json={"channel_id": 1, "title": "Remove this thread"},
        headers={"x-csrf-token": token},
    )
    active_response = await client.post(
        "/studio/api/conversations",
        json={"channel_id": 1, "title": "Keep this thread"},
        headers={"x-csrf-token": token},
    )
    assert target_response.status_code == active_response.status_code == 200
    target_id = target_response.json()["conversation"]["id"]
    active_id = uuid.UUID(active_response.json()["conversation"]["id"])
    repository = app.state.studio_repository

    target_draft = await repository.create_draft(
        conversation_id=uuid.UUID(target_id),
        channel_id=1,
        payload={"body": "Delete this Studio artifact", "creative": True},
    )
    message = await repository.append_message(
        conversation_id=active_id, role="user", content="Active run"
    )
    run = await repository.create_run(
        conversation_id=active_id, user_message_id=message["id"], requested_model="test"
    )

    missing_csrf = await client.delete(f"/studio/api/conversations/{target_id}")
    assert missing_csrf.status_code == 403

    blocked = await client.delete(
        f"/studio/api/conversations/{active_id}", headers={"x-csrf-token": token}
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "conversation_active_run"

    await repository.set_run_status(run["id"], status="succeeded")
    deleted = await client.delete(
        f"/studio/api/conversations/{target_id}", headers={"x-csrf-token": token}
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"conversation_id": target_id, "deleted": True}

    listed = await client.get("/studio/api/conversations")
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["conversations"]] == [str(active_id)]
    messages = await client.get(f"/studio/api/conversations/{target_id}/messages")
    draft = await client.get(f"/studio/api/drafts/{target_draft['id']}")
    assert messages.status_code == draft.status_code == 404
    repeated = await client.delete(
        f"/studio/api/conversations/{target_id}", headers={"x-csrf-token": token}
    )
    assert repeated.status_code == 404


def test_conversation_delete_ui_contract_is_confirmed_and_accessible():
    source = (ROOT / "studio-frontend/src/main.tsx").read_text(encoding="utf-8")
    styles = (ROOT / "studio-frontend/src/styles.css").read_text(encoding="utf-8")
    assert "window.confirm" in source
    assert "permanently" in source
    assert 'method: "DELETE"' in source
    assert "/studio/api/conversations/${encodeURIComponent(conversation.id)}" in source
    assert "aria-label={`Delete conversation ${conversation.title}`}" in source
    assert ".studio-conversation-delete" in styles


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_delete_cascades_studio_records_and_rejects_active_runs():
    database_url = os.environ.get("M7_POSTGRES_URL")
    if not database_url:
        pytest.skip("set M7_POSTGRES_URL to run the PostgreSQL conversation-delete proof")

    from app.postgres_db import PostgresDatabase

    workspace_slug = f"m7-delete-{uuid.uuid4().hex[:10]}"
    db = PostgresDatabase(database_url, workspace_slug=workspace_slug)
    try:
        await db.init_db(admin_username=f"{workspace_slug}-admin")
        channel_id = await db.upsert_channel(f"@{workspace_slug}", "Delete fixture", 7301)
        repository = StudioRepository(db)
        conversation = await repository.create_conversation(channel_id=channel_id, title="Delete fixture")
        source_id = f"m7-source-{uuid.uuid4().hex[:12]}"
        await repository.persist_research_bundle(
            conversation_id=conversation["id"],
            channel_id=channel_id,
            bundle={
                "sources": [{"source_id": source_id, "url": "https://news.test/m7"}],
                "stories": [{"cluster_id": "m7-story", "headline": "Delete fixture", "source_ids": [source_id]}],
            },
        )
        await repository.record_research_event(
            conversation_id=conversation["id"],
            channel_id=channel_id,
            event={"provider": "test", "trace_id": "m7-delete-trace"},
        )
        draft = await repository.create_draft(
            conversation_id=conversation["id"],
            channel_id=channel_id,
            payload={"body": "Studio-only draft", "creative": True},
        )
        message = await repository.append_message(
            conversation_id=conversation["id"], role="user", content="Delete this fixture"
        )
        run = await repository.create_run(
            conversation_id=conversation["id"], user_message_id=message["id"], requested_model="test"
        )
        await repository.append_event(run["id"], event_type="RUN_STARTED")
        with pytest.raises(ActiveRunExists):
            await repository.delete_conversation(conversation["id"])

        await repository.set_run_status(run["id"], status="succeeded")
        assert await repository.delete_conversation(conversation["id"]) is True
        assert await repository.get_conversation(conversation["id"]) is None
        assert await repository.get_draft(draft["id"]) is None
        assert await repository.get_research_bundle(conversation_id=conversation["id"], channel_id=channel_id) is None
        assert await repository.list_messages(conversation["id"]) == []
        assert await repository.get_events(run["id"]) == []
        for table in (
            "studio_messages",
            "studio_agent_runs",
            "studio_run_events",
            "studio_drafts",
            "studio_draft_versions",
            "studio_sources",
            "studio_story_clusters",
            "studio_research_events",
        ):
            result = await db._execute(
                f"SELECT count(*) FROM {table} WHERE workspace_id=:workspace_id",
                {"workspace_id": db.workspace_id},
            )
            assert result.scalar_one() == 0, table
    finally:
        await db.close()
