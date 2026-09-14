import os
import re
from pathlib import Path
import uuid

import pytest
from sqlalchemy import text

from app.postgres_db import PostgresDatabase
from app.studio.repository import MemoryStudioRepository, StudioRepository


async def _login_and_csrf(client, settings) -> str:
    await client.post(
        "/login",
        data={"username": settings.admin_username, "password": settings.admin_password},
    )
    home = await client.get("/studio")
    return re.search(r'<meta name="studio-csrf-token" content="([^"]+)"', home.text).group(1)


def _second_channel(repository: MemoryStudioRepository) -> None:
    repository.channels.append(
        {"id": 2, "identifier": "@second_channel", "title": "Second channel", "active": True}
    )


@pytest.mark.asyncio
async def test_repository_separates_prompts_conversations_and_message_memory():
    repository = MemoryStudioRepository()
    _second_channel(repository)
    first = await repository.create_conversation(channel_id=1, title="First history")
    second = await repository.create_conversation(channel_id=2, title="Second history")
    await repository.append_message(
        conversation_id=first["id"], role="user", content="Private to first"
    )
    await repository.append_message(
        conversation_id=second["id"], role="user", content="Private to second"
    )
    await repository.set_system_prompt(1, "First-channel rules")
    await repository.set_system_prompt(2, "Second-channel rules")

    assert [row["id"] for row in await repository.list_conversations(channel_id=1)] == [first["id"]]
    assert [row["id"] for row in await repository.list_conversations(channel_id=2)] == [second["id"]]
    assert [row["content"] for row in await repository.list_messages(first["id"])] == ["Private to first"]
    assert [row["content"] for row in await repository.list_messages(second["id"])] == ["Private to second"]
    assert await repository.get_system_prompt(1) == "First-channel rules"
    assert await repository.get_system_prompt(2) == "Second-channel rules"


@pytest.mark.asyncio
async def test_bootstrap_and_settings_are_selected_channel_only(client, settings, app):
    settings.studio_test_mode = True
    token = await _login_and_csrf(client, settings)
    repository = app.state.studio_repository
    _second_channel(repository)

    first = await repository.create_conversation(channel_id=1, title="First conversation")
    second = await repository.create_conversation(channel_id=2, title="Second conversation")
    await repository.append_message(
        conversation_id=first["id"], role="user", content="First-channel memory"
    )
    await repository.append_message(
        conversation_id=second["id"], role="user", content="Second-channel memory"
    )
    await repository.upsert_profile_text(
        {
            "channel_id": 1,
            "expected_version": 0,
            "topics_text": "First topic",
            "editorial_text": "",
            "style_text": "",
        }
    )
    await repository.upsert_profile_text(
        {
            "channel_id": 2,
            "expected_version": 0,
            "topics_text": "Second topic",
            "editorial_text": "",
            "style_text": "",
        }
    )
    await repository.set_system_prompt(1, "First prompt")
    await repository.set_system_prompt(2, "Second prompt")

    response = await client.get("/studio/api/bootstrap?channel_id=2")
    assert response.status_code == 200
    payload = response.json()
    assert payload["selected_channel_id"] == 2
    assert [item["id"] for item in payload["conversations"]] == [str(second["id"])]
    assert payload["current_conversation"]["channel_id"] == 2
    assert payload["profile"]["topics_text"] == "Second topic"
    assert "First topic" not in str(payload)
    assert "First-channel memory" not in str(payload)

    prompt = await client.get("/studio/api/settings?channel_id=2")
    assert prompt.json() == {"channel_id": 2, "system_prompt": "Second prompt"}
    changed = await client.patch(
        "/studio/api/settings",
        headers={"x-csrf-token": token},
        json={"channel_id": 2, "system_prompt": "Updated second prompt"},
    )
    assert changed.status_code == 200
    assert await repository.get_system_prompt(1) == "First prompt"
    assert await repository.get_system_prompt(2) == "Updated second prompt"


def test_studio_ui_exposes_channel_selector_and_channel_scoped_prompt_copy():
    source = (Path(__file__).resolve().parents[1] / "studio-frontend/src/main.tsx").read_text(encoding="utf-8")
    assert 'aria-label="Studio channel"' in source
    assert "/studio/api/bootstrap?channel_id=" in source
    assert "They never apply to another channel." in source
    assert "conversations only" in source


def test_channel_prompt_migration_backfills_before_prompts_diverge():
    migration = (Path(__file__).resolve().parents[1] / "alembic/versions/0012_channel_system_prompts.py").read_text(encoding="utf-8")
    assert 'revision = "0012_channel_system_prompts"' in migration
    assert 'down_revision = "0011_tool_result_logs"' in migration
    assert "workspace.studio_system_prompt" in migration
    assert 'op.drop_column("channels", "studio_system_prompt")' in migration


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_channel_prompts_and_conversation_indexes_are_isolated():
    database_url = os.getenv("TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("set TEST_POSTGRES_URL to run the channel-isolation PostgreSQL proof")
    slug = f"channel-isolation-{uuid.uuid4().hex[:10]}"
    db = PostgresDatabase(database_url, workspace_slug=slug)
    await db.init_db(admin_username=f"{slug}-admin")
    try:
        first_channel = await db.add_channel(f"@{slug.replace('-', '')}a")
        second_channel = await db.add_channel(f"@{slug.replace('-', '')}b")
        repository = StudioRepository(db)
        await repository.set_system_prompt(first_channel, "Only first")
        await repository.set_system_prompt(second_channel, "Only second")
        first = await repository.create_conversation(channel_id=first_channel, title="First")
        second = await repository.create_conversation(channel_id=second_channel, title="Second")

        assert await repository.get_system_prompt(first_channel) == "Only first"
        assert await repository.get_system_prompt(second_channel) == "Only second"
        assert [row["id"] for row in await repository.list_conversations(channel_id=first_channel)] == [first["id"]]
        assert [row["id"] for row in await repository.list_conversations(channel_id=second_channel)] == [second["id"]]
    finally:
        async with db.sessions.session() as session:
            await session.execute(text("DELETE FROM workspaces WHERE slug=:slug"), {"slug": slug})
            await session.commit()
        await db.close()
